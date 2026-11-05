"""Build a local reference gallery from selected, CC-BY open-access articles."""

from __future__ import annotations

import concurrent.futures
import html
import io
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from lxml import etree
from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "图片"
DATE = "2026-11-05"
USER_AGENT = "ResearchFigureReferenceGallery/1.0 (educational local collection)"
SELECTED = {
    "果蝇与感觉": [
        "PMC11446842", "PMC11446831", "PMC11446845", "PMC11525180",
        "PMC11446846", "PMC12119369", "PMC11621569", "PMC8105414",
        "PMC7893153", "PMC11228034", "PMC12048489", "PMC12559195",
    ],
    "神经环路与决策": [
        "PMC11499283", "PMC11903320", "PMC12408363", "PMC12408349",
        "PMC10172131", "PMC10665198", "PMC9892006", "PMC9360402",
        "PMC7229188", "PMC6662698", "PMC10015009", "PMC8408193",
    ],
    "脑启发与计算模型": [
        "PMC10803782", "PMC8455577", "PMC7426273", "PMC7367848",
        "PMC10917819", "PMC9068614", "PMC7814066", "PMC8791835",
    ],
    "网络与复杂系统": [
        "PMC9649740", "PMC8342694", "PMC7930026", "PMC6465316",
        "PMC6707263", "PMC10761873", "PMC8149673", "PMC11161514",
    ],
    "群体行为与动力学": [
        "PMC8590009", "PMC7264142", "PMC8748659", "PMC9090766",
    ],
    "脑图谱与成像": [
        "PMC9021021", "PMC10060165", "PMC8497570", "PMC6814749",
        "PMC9381504", "PMC7801665", "PMC9395399", "PMC9038754",
    ],
}

TYPE_PATTERNS = {
    "示意图 / 方法": r"\b(schematic|illustrat(?:ion|ed)|diagram|workflow|pipeline|framework|experimental (?:design|setup|paradigm))\b",
    "网络 / 连接组": r"\b(network|connectome|connectivity|graph|connectomic|neuronal circuit)\b",
    "曲线 / 时间序列": r"\b(time (?:course|series)|trace|trajector\w*|dynamics|temporal|over time|response curve)\b",
    "热图 / 矩阵": r"\b(heat\s?map|matrix|matrices|correlation map)\b",
    "散点 / 降维": r"\b(scatter\s?plot|scatter plot|embedding|principal component|umap|t-sne|dimensionality reduction)\b",
    "显微 / 脑成像": r"\b(microscop\w*|imaging|fluorescen\w*|mri|confocal|brain images|stain\w*)\b",
    "统计 / 分布": r"\b(box ?plots?|violin plots?|bar plots?|histograms?|distributions?|statistical|quantification)\b",
}

# Exclude figures with indications of third-party artwork or a separate reuse credit.
EXTERNAL_CREDIT = re.compile(
    r"adapted from|modified from|reproduced from|reprinted from|"
    r"permission of|with permission|biorender|image credit|"
    r"copyright|creative commons|©|licensed under",
    re.IGNORECASE,
)
XLINK = "{http://www.w3.org/1999/xlink}href"


def get(url: str, attempts: int = 3) -> bytes:
    error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=35) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = exc
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"{url}: {error}")


def words(node) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def article(category: str, pmcid: str):
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(get(url), parser)
    meta = root.find("./front/article-meta")
    if meta is None:
        raise ValueError(f"{pmcid}: missing article metadata")

    license_links = meta.xpath("./permissions/license//ext-link/@xlink:href", namespaces={"xlink": "http://www.w3.org/1999/xlink"})
    license_url = next((link for link in license_links if re.search(r"creativecommons\.org/licenses/by/4\.0/?", link)), None)
    if not license_url:
        raise ValueError(f"{pmcid}: no explicit CC BY 4.0 article license")
    license_url = "https://creativecommons.org/licenses/by/4.0/"

    title = words(meta.find("./title-group/article-title"))
    journal = words(root.find("./front/journal-meta/journal-title-group/journal-title"))
    doi = words(meta.find("./article-id[@pub-id-type='doi']"))
    year = next((words(y) for y in meta.xpath("./pub-date/year") if words(y)), "")
    first_author = meta.find("./contrib-group/contrib[@contrib-type='author']/name")
    surname = words(first_author.find("surname")) if first_author is not None else ""
    author = (surname + " et al.") if surname else "See paper"
    if not (title and doi and year):
        raise ValueError(f"{pmcid}: incomplete citation")

    results = []
    media_urls = None
    for fig in root.xpath("./body//fig[not(ancestor::fig)]"):
        fig_id = fig.get("id", "")
        label = words(fig.find("label"))
        caption = fig.find("caption")
        caption_text = words(caption)
        if not fig_id or not label or not caption_text or fig.find("permissions") is not None:
            continue
        if EXTERNAL_CREDIT.search(caption_text):
            continue
        graphic = None
        for candidate in fig.xpath(".//graphic"):
            href = candidate.get(XLINK, "")
            pi = candidate.xpath("processing-instruction('cloudpmc-path')")
            if not re.search(r"\.(jpe?g|png|webp|tiff?)$", href, re.I):
                continue
            if not pi:
                urn = candidate.xpath("processing-instruction('image-cloudpmc-urn')")
                if urn and urn[0].text.strip().startswith("urn:cdn:blobs/"):
                    graphic = "https://cdn.ncbi.nlm.nih.gov/pmc/" + quote(urn[0].text.strip()[len("urn:cdn:"):], safe="/")
                    break
            if pi:
                path = pi[0].text.strip()
                if path.startswith("blobs/"):
                    graphic = "https://cdn.ncbi.nlm.nih.gov/pmc/" + quote(path, safe="/")
                    break
            if media_urls is None:
                page = get(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/").decode("utf-8", errors="replace")
                found_urls = re.findall(r"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^\s\"'<>]+", page)
                media_urls = {unquote(Path(urlsplit(html.unescape(url)).path).name): html.unescape(url) for url in found_urls}
            if href in media_urls:
                graphic = media_urls[href]
                break
        if graphic is None:
            continue
        figure_title = words(caption.find("title")) or caption_text[:180]
        tags = [label for label, pattern in TYPE_PATTERNS.items() if re.search(pattern, caption_text, re.I)]
        filename = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{pmcid}-{fig_id}") + ".webp"
        results.append({
            "id": f"{pmcid}-{fig_id}", "category": category, "paper": title,
            "journal": journal, "year": year, "author": author, "doi": doi,
            "pmcid": pmcid, "label": label, "figure_title": figure_title,
            "caption": caption_text, "tags": tags, "image": f"图片/{filename}",
            "original": graphic,
            "paper_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/#{quote(fig_id)}",
            "doi_url": f"https://doi.org/{doi}", "license": license_url,
        })
    return results


def save_image(figure):
    target = ROOT / figure["image"]
    if target.exists():
        try:
            with Image.open(target) as previous:
                previous.verify()
            return figure
        except OSError:
            target.unlink()
    content = get(figure["original"])
    with Image.open(io.BytesIO(content)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = image.convert("RGB")
        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        image.save(target, "WEBP", quality=90, method=4)
    return figure


def write_obsidian_pages(figures):
    wall_dir = ROOT / "图墙"
    wall_dir.mkdir(exist_ok=True)
    links = []
    for number, category in enumerate(SELECTED, start=1):
        selected = [fig for fig in figures if fig["category"] == category]
        filename = f"{number:02d}_{category}.md"
        links.append((category, filename, len(selected)))
        lines = [f"# {category}", "", f"> 共 {len(selected)} 张。图片是按原图缩放的本地预览；查看原文可获取完整图注和高清版本。", ""]
        for fig in selected:
            title = fig["figure_title"].replace("[", "（").replace("]", "）").replace("\n", " ")
            lines += [
                f"## {fig['label']} · {title}", "",
                f"![{fig['label']}|680](../{fig['image']})", "",
                f"{fig['author']} · *{fig['journal']}* · {fig['year']}  ",
                f"[{fig['paper']}]({fig['paper_url']}) · [原始图片]({fig['original']}) · [CC BY 4.0]({fig['license']})", "",
            ]
        (wall_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")
    paper_count = len({fig["pmcid"] for fig in figures})
    nature_count = len({fig["pmcid"] for fig in figures if fig["journal"].lower() == "nature"})
    lines = [
        "# 科研绘图大图库", "",
        f"> 截至 {DATE}：{len(figures)} 张本地图例，来自 {paper_count} 篇论文，其中 {nature_count} 篇为 Nature 正刊。", "",
        "## 两种浏览方式", "",
        "- **网页筛选**：[打开科研绘图大图库](01_科研绘图大图库.html)。可按关键词、研究主题和期刊筛选图片，并放大查看。",
        "- **Obsidian 图墙**：直接打开下方分类，在笔记中逐图浏览。图片保存在同目录的 `图片` 文件夹，离线可看。", "",
        "## 主题图墙", "",
    ]
    lines += [f"- [{category}](图墙/{filename})：{count} 张" for category, filename, count in links]
    lines += [
        "", "## 继续扩充", "",
        "- [科研图片资源索引](02_资源与审美笔记.md)：论文图像、学术图集与科研插画素材库。",
        "- [来源清单](来源清单.json)：可检索的原论文、图注、原始图片、主题与许可信息。", "",
        "## 说明", "",
        "论文的文章级许可均为 CC BY 4.0，图片为原始图的缩放预览。图内若有第三方材料，其单独标注的许可优先；公开传播或复用前，请检查图注和原文。研究主题根据论文选择，网页中的图形线索由英文图注关键词自动识别，可能漏标或误标。", "",
    ]
    (ROOT / "00_从这里开始.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [(category, pmcid) for category, ids in SELECTED.items() for pmcid in ids]
    figures = []
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        future_jobs = {pool.submit(article, category, pmcid): (category, pmcid) for category, pmcid in jobs}
        for future in concurrent.futures.as_completed(future_jobs):
            category, pmcid = future_jobs[future]
            try:
                found = future.result()
                figures.extend(found)
                print(f"ARTICLE {pmcid} {len(found)} figures", flush=True)
            except Exception as exc:
                failures.append({"stage": "article", "id": pmcid, "error": str(exc)})
                print(f"SKIP {pmcid}: {exc}", flush=True)

    # Show main figures from different papers before supplementary figures.
    order = {pmcid: number for number, (_, pmcid) in enumerate(jobs)}
    figures.sort(key=lambda item: ("extended" in item["label"].lower(), int(re.search(r"\d+", item["label"]).group()) if re.search(r"\d+", item["label"]) else 999, order[item["pmcid"]]))
    saved_ids = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        future_images = {pool.submit(save_image, fig): fig for fig in figures}
        for number, future in enumerate(concurrent.futures.as_completed(future_images), start=1):
            fig = future_images[future]
            try:
                saved_ids.add(future.result()["id"])
            except Exception as exc:
                failures.append({"stage": "image", "id": fig["id"], "error": str(exc)})
                print(f"IMAGE SKIP {fig['id']}: {exc}", flush=True)
            if number % 25 == 0 or number == len(figures):
                print(f"IMAGES {number}/{len(figures)}", flush=True)
    figures = [fig for fig in figures if fig["id"] in saved_ids]
    data = {"created": DATE, "figures": figures}
    (ROOT / "图片数据.js").write_text("window.GALLERY_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    (ROOT / "来源清单.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "采集记录.json").write_text(json.dumps({"date": DATE, "selected": SELECTED, "failures": failures}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_obsidian_pages(figures)
    print(f"DONE {len(figures)} figures / {len(set(f['pmcid'] for f in figures))} papers / {len(failures)} failures", flush=True)


if __name__ == "__main__":
    main()
