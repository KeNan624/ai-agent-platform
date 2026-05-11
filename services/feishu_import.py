from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app_config import get_app_setting
from services.cos_upload import CosUploadError, upload_bytes


class FeishuImportError(RuntimeError):
    """Raised when a Feishu document cannot be imported."""


@dataclass
class FeishuImportedDocument:
    title: str
    markdown: str
    image_count: int = 0
    style_count: int = 0
    callout_count: int = 0
    warnings: list[str] = field(default_factory=list)


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
TEXT_FIELD_BY_TYPE = {
    2: "text",
    3: "heading1",
    4: "heading2",
    5: "heading3",
    6: "heading4",
    7: "heading5",
    8: "heading6",
    9: "heading7",
    10: "heading8",
    11: "heading9",
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    17: "todo",
}
TEXT_FIELDS = set(TEXT_FIELD_BY_TYPE.values())
HEADING_FIELDS = {f"heading{i}" for i in range(1, 10)}
IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def parse_feishu_docx_id(url_or_token: str) -> str:
    raw = (url_or_token or "").strip()
    if not raw:
        raise FeishuImportError("请填写飞书文档链接")
    if "/wiki/" in raw:
        raise FeishuImportError("暂不支持飞书知识库 wiki 链接，请复制原始 docx 文档链接")

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        match = re.search(r"/docx/([^/?#]+)", parsed.path)
        if not match:
            raise FeishuImportError("第一版只支持飞书新版文档 docx 链接")
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", raw):
        return raw
    raise FeishuImportError("飞书文档链接格式不正确")


def _required_setting(key: str, label: str) -> str:
    value = (get_app_setting(key, "") or "").strip()
    if not value:
        raise FeishuImportError(f"飞书未配置完整，请在后台配置 {label}")
    return value


async def _api_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = await client.request(
            method,
            f"{FEISHU_API_BASE}{path}",
            headers=headers,
            params=params,
            json=json,
        )
    except httpx.HTTPError as exc:
        raise FeishuImportError(f"飞书接口请求失败：{str(exc)[:160]}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuImportError(f"飞书接口返回异常：HTTP {response.status_code}") from exc
    if response.status_code >= 400 or payload.get("code") not in (0, None):
        msg = payload.get("msg") or payload.get("message") or response.text[:200]
        if response.status_code in {401, 403} or str(payload.get("code", "")).startswith("9"):
            raise FeishuImportError(f"应用无权访问该文档或权限未开通：{msg}")
        raise FeishuImportError(f"飞书接口调用失败：{msg}")
    return payload.get("data") or payload


async def _tenant_access_token(client: httpx.AsyncClient) -> str:
    app_id = _required_setting("FEISHU_APP_ID", "FEISHU_APP_ID")
    app_secret = _required_setting("FEISHU_APP_SECRET", "FEISHU_APP_SECRET")
    data = await _api_json(
        client,
        "POST",
        "/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    token = data.get("tenant_access_token")
    if not token:
        raise FeishuImportError("飞书 tenant_access_token 获取失败")
    return token


def _block_id(block: dict[str, Any]) -> str:
    return str(block.get("block_id") or block.get("id") or "").strip()


def _block_type(block: dict[str, Any]) -> int | str | None:
    raw = block.get("block_type")
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return raw


def _child_ids(block: dict[str, Any]) -> list[str]:
    raw = block.get("children") or block.get("children_id") or block.get("child_ids") or []
    ids: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            cid = _block_id(item)
            if cid:
                ids.append(cid)
    return ids


async def _get_document_info(client: httpx.AsyncClient, token: str, document_id: str) -> dict[str, Any]:
    return await _api_json(client, "GET", f"/docx/v1/documents/{quote(document_id, safe='')}", token=token)


async def _list_document_blocks(client: httpx.AsyncClient, token: str, document_id: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 500, "document_revision_id": -1}
        if page_token:
            params["page_token"] = page_token
        data = await _api_json(
            client,
            "GET",
            f"/docx/v1/documents/{quote(document_id, safe='')}/blocks",
            token=token,
            params=params,
        )
        items = data.get("items") or data.get("blocks") or data.get("block_list") or []
        if not isinstance(items, list):
            raise FeishuImportError("飞书文档块数据格式异常")
        blocks.extend(item for item in items if isinstance(item, dict))
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            break
    if not blocks:
        raise FeishuImportError("飞书文档没有可导入内容")
    return blocks


def _extract_document_title(info: dict[str, Any]) -> str:
    candidates = [
        info.get("title"),
        (info.get("document") or {}).get("title") if isinstance(info.get("document"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _escape_table_cell(text: str) -> str:
    return " ".join((text or "").replace("|", "\\|").split())


def _markdown_text_escape(text: str) -> str:
    return (text or "").replace("\u00a0", " ")


def _html_text(text: str) -> str:
    return html.escape((text or "").replace("\u00a0", " "), quote=False).replace("\n", "<br>")


def _html_attr(value: str) -> str:
    return html.escape(value or "", quote=True)


def _safe_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return ""
    return raw


def _style_number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= number <= 99:
        return number
    return None


def _style_class(value: Any, prefix: str) -> str:
    number = _style_number(value)
    return f"{prefix}-{number}" if number is not None else ""


def _extract_image_token(block: dict[str, Any]) -> str:
    image = block.get("image") or {}
    if not isinstance(image, dict):
        return ""
    for key in ("token", "file_token", "image_token", "media_id", "fileToken"):
        value = image.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in image.values():
        if isinstance(value, dict):
            for key in ("token", "file_token", "image_token", "media_id", "fileToken"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


class FeishuMarkdownRenderer:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        document_id: str,
        blocks: list[dict[str, Any]],
        user_id: int,
    ) -> None:
        self.client = client
        self.token = token
        self.document_id = document_id
        self.blocks = blocks
        self.user_id = user_id
        self.blocks_by_id = {_block_id(block): block for block in blocks if _block_id(block)}
        self.warnings: list[str] = []
        self._warning_set: set[str] = set()
        self.image_count = 0
        self.style_count = 0
        self.callout_count = 0
        self.first_heading = ""

    def warn(self, message: str) -> None:
        if message in self._warning_set:
            return
        self._warning_set.add(message)
        self.warnings.append(message)

    def _root_ids(self) -> list[str]:
        for block in self.blocks:
            if _block_id(block) == self.document_id or _block_type(block) == 1:
                ids = _child_ids(block)
                if ids:
                    return ids
        child_set = {cid for block in self.blocks for cid in _child_ids(block)}
        return [
            _block_id(block)
            for block in self.blocks
            if _block_id(block) and _block_id(block) not in child_set and _block_type(block) != 1
        ]

    async def render(self) -> str:
        parts: list[str] = []
        for block_id in self._root_ids():
            block = self.blocks_by_id.get(block_id)
            if not block:
                continue
            rendered = await self.render_block(block)
            if rendered.strip():
                parts.append(rendered.strip())
        markdown = "\n\n".join(parts).strip()
        if not markdown:
            raise FeishuImportError("飞书文档未解析出正文内容")
        return markdown

    async def render_block(self, block: dict[str, Any], *, compact: bool = False) -> str:
        block_type = _block_type(block)
        if block_type == 22 or "divider" in block:
            return "---"
        if block_type == 19 or "callout" in block:
            return await self.render_callout(block)
        if block_type == 34 or "quote_container" in block:
            return await self.render_quote_container(block)
        if block_type == 27 or "image" in block:
            return await self.render_image(block)
        if block_type == 31 or "table" in block:
            return await self.render_table(block)
        if any(key in block for key in ("video", "file", "bitable", "mindnote", "board")):
            self.warn("已跳过视频、附件、流程图、画板等暂不支持的飞书块")
            return ""

        field_name = self._text_field_name(block)
        if field_name:
            rendered_text = self.render_text_like(block, field_name, compact=compact)
            rendered_children = await self.render_child_blocks(block, compact=compact)
            return self.join_rendered_parts(rendered_text, rendered_children, compact=compact)

        rendered_children = await self.render_child_blocks(block, compact=compact)
        if rendered_children:
            return rendered_children
        if block_type not in (1, None):
            self.warn(f"暂不支持的飞书块类型：{block_type}")
        return ""

    async def render_child_blocks(self, block: dict[str, Any], *, compact: bool = False) -> str:
        child_parts = []
        for child_id in _child_ids(block):
            child = self.blocks_by_id.get(child_id)
            if child:
                child_parts.append(await self.render_block(child, compact=compact))
        separator = " " if compact else "\n\n"
        return separator.join(part.strip() for part in child_parts if part.strip())

    def join_rendered_parts(self, first: str, second: str, *, compact: bool = False) -> str:
        first = (first or "").strip()
        second = (second or "").strip()
        if first and second:
            return f"{first}{' ' if compact else '\n\n'}{second}"
        return first or second

    def _text_field_name(self, block: dict[str, Any]) -> str:
        block_type = _block_type(block)
        mapped = TEXT_FIELD_BY_TYPE.get(block_type) if isinstance(block_type, int) else None
        if mapped and isinstance(block.get(mapped), dict):
            return mapped
        for key in TEXT_FIELDS:
            if isinstance(block.get(key), dict):
                return key
        return ""

    def render_text_like(self, block: dict[str, Any], field_name: str, *, compact: bool = False) -> str:
        content = block.get(field_name) or {}
        if self._block_style_needs_html(content.get("style") or {}):
            return self.render_text_like_html(block, field_name, compact=compact)
        text = self.render_inline(content.get("elements") or [])
        if not text.strip():
            return ""
        text = _markdown_text_escape(text.strip())
        if field_name in HEADING_FIELDS:
            level = max(1, min(6, int(field_name.replace("heading", ""))))
            if not self.first_heading:
                self.first_heading = re.sub(r"<[^>]+>", "", text).strip("# *")
            return f"{'#' * level} {text}"
        if field_name == "bullet":
            return f"- {text}"
        if field_name == "ordered":
            return f"1. {text}"
        if field_name == "todo":
            done = bool(content.get("style", {}).get("done") or content.get("done"))
            return f"- [{'x' if done else ' '}] {text}"
        if field_name == "quote":
            return "\n".join(f"> {line}" for line in text.splitlines() or [text])
        if field_name == "code":
            language = content.get("style", {}).get("language") or ""
            return f"```{language}\n{text}\n```"
        return text if compact else text

    def render_text_like_html(self, block: dict[str, Any], field_name: str, *, compact: bool = False) -> str:
        content = block.get(field_name) or {}
        inner = self.render_inline(content.get("elements") or [], html_mode=True).strip()
        if not inner:
            return ""
        style = content.get("style") if isinstance(content.get("style"), dict) else {}
        classes = self._block_classes(style)
        if classes:
            self.style_count += 1
        class_attr = f' class="{_html_attr(" ".join(classes))}"' if classes else ""
        if field_name in HEADING_FIELDS:
            level = max(1, min(6, int(field_name.replace("heading", ""))))
            if not self.first_heading:
                self.first_heading = re.sub(r"<[^>]+>", "", inner)
            return f"<h{level}{class_attr}>{inner}</h{level}>"
        if field_name == "bullet":
            return f"<ul{class_attr}><li>{inner}</li></ul>"
        if field_name == "ordered":
            return f"<ol{class_attr}><li>{inner}</li></ol>"
        if field_name == "todo":
            done = bool(style.get("done") or content.get("done"))
            return f'<ul{class_attr}><li>{"☑" if done else "☐"} {inner}</li></ul>'
        if field_name == "quote":
            return f"<blockquote{class_attr}>{inner}</blockquote>"
        if field_name == "code":
            language = _html_attr(str(style.get("language") or ""))
            return f'<pre{class_attr}><code class="language-{language}">{inner}</code></pre>'
        tag = "span" if compact else "p"
        return f"<{tag}{class_attr}>{inner}</{tag}>"

    def render_inline(self, elements: list[Any], *, html_mode: bool = False) -> str:
        chunks: list[str] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            text, style, link = self._inline_piece(element)
            if not text:
                continue
            if html_mode or self._inline_style_needs_html(style):
                if self._inline_style_needs_html(style):
                    self.style_count += 1
                chunks.append(self._inline_html(text, style, link))
                continue
            text = _markdown_text_escape(text)
            if style.get("inline_code"):
                text = f"`{text}`"
            if style.get("bold"):
                text = f"**{text}**"
            if style.get("italic"):
                text = f"*{text}*"
            if style.get("strikethrough"):
                text = f"~~{text}~~"
            if link:
                safe_link = _safe_url(link)
                if safe_link:
                    text = f"[{text}]({safe_link})"
            chunks.append(text)
        return "".join(chunks)

    def _inline_style_needs_html(self, style: dict[str, Any]) -> bool:
        return bool(
            style.get("underline")
            or _style_number(style.get("text_color") or style.get("font_color")) is not None
            or _style_number(style.get("background_color")) is not None
        )

    def _block_style_needs_html(self, style: dict[str, Any]) -> bool:
        return bool(
            _style_number(style.get("background_color")) is not None
            or str(style.get("align") or style.get("text_align") or "").lower() in {"2", "3", "center", "right"}
        )

    def _block_classes(self, style: dict[str, Any]) -> list[str]:
        classes: list[str] = []
        bg_class = _style_class(style.get("background_color"), "fs-bg")
        if bg_class:
            classes.append(bg_class)
            classes.append("fs-block-highlight")
        align = str(style.get("align") or style.get("text_align") or "").lower()
        if align in {"2", "center"}:
            classes.append("fs-align-center")
        elif align in {"3", "right"}:
            classes.append("fs-align-right")
        return classes

    def _inline_html(self, text: str, style: dict[str, Any], link: str = "") -> str:
        rendered = _html_text(text)
        if style.get("inline_code"):
            rendered = f"<code>{rendered}</code>"
        if style.get("bold"):
            rendered = f"<strong>{rendered}</strong>"
        if style.get("italic"):
            rendered = f"<em>{rendered}</em>"
        if style.get("strikethrough"):
            rendered = f"<s>{rendered}</s>"
        if style.get("underline"):
            rendered = f"<u>{rendered}</u>"
        classes = [
            cls
            for cls in (
                _style_class(style.get("text_color") or style.get("font_color"), "fs-fg"),
                _style_class(style.get("background_color"), "fs-bg"),
            )
            if cls
        ]
        if classes:
            rendered = f'<span class="{_html_attr(" ".join(classes))}">{rendered}</span>'
        safe_link = _safe_url(link)
        if safe_link:
            rendered = f'<a href="{_html_attr(safe_link)}" target="_blank" rel="noopener noreferrer">{rendered}</a>'
        return rendered

    def _inline_piece(self, element: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        for key in ("text_run", "mention_user", "mention_doc", "docs_link"):
            value = element.get(key)
            if not isinstance(value, dict):
                continue
            text = (
                value.get("content")
                or value.get("text")
                or value.get("title")
                or value.get("name")
                or ""
            )
            style = value.get("text_element_style") or value.get("style") or {}
            link_obj = style.get("link") if isinstance(style, dict) else None
            link = ""
            if isinstance(link_obj, dict):
                link = str(link_obj.get("url") or "")
            elif isinstance(link_obj, str):
                link = link_obj
            link = str(value.get("url") or value.get("href") or link or "")
            return str(text), style if isinstance(style, dict) else {}, link
        equation = element.get("equation")
        if isinstance(equation, dict):
            return str(equation.get("content") or ""), {}, ""
        return "", {}, ""

    async def render_image(self, block: dict[str, Any]) -> str:
        url = await self.upload_image(block)
        return f"![图片]({url})" if url else ""

    async def upload_image(self, block: dict[str, Any]) -> str:
        token = _extract_image_token(block)
        if not token:
            self.warn("发现图片块但没有拿到素材 token，已跳过")
            return ""
        try:
            response = await self.client.get(
                f"{FEISHU_API_BASE}/drive/v1/medias/{quote(token, safe='')}/download",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            raise FeishuImportError(f"飞书图片下载失败：{str(exc)[:160]}") from exc
        if response.status_code >= 400:
            raise FeishuImportError(f"飞书图片下载失败：HTTP {response.status_code}")
        content_type = (response.headers.get("Content-Type") or "image/png").split(";")[0].lower()
        if content_type == "image/jpg":
            content_type = "image/jpeg"
        if content_type == "application/json":
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise FeishuImportError(f"飞书图片下载失败：{payload.get('msg') or '接口返回错误'}")
        ext = IMAGE_CONTENT_TYPES.get(content_type, ".png")
        try:
            uploaded = await upload_bytes(
                response.content,
                filename=f"feishu-{token[:12]}{ext}",
                kind="image",
                user_id=self.user_id,
                content_type=content_type,
            )
        except CosUploadError as exc:
            raise FeishuImportError(str(exc)) from exc
        self.image_count += 1
        return str(uploaded["url"])

    async def render_callout(self, block: dict[str, Any]) -> str:
        callout = block.get("callout") if isinstance(block.get("callout"), dict) else {}
        self.callout_count += 1
        classes = ["fs-callout"]
        bg_class = _style_class(callout.get("background_color"), "fs-callout-bg")
        border_class = _style_class(callout.get("border_color"), "fs-callout-border")
        text_class = _style_class(callout.get("text_color"), "fs-fg")
        if bg_class:
            classes.append(bg_class)
        if border_class:
            classes.append(border_class)
        if text_class:
            classes.append(text_class)
        icon = self._callout_icon(callout)
        content_html = await self.render_children_as_html(block)
        if not content_html:
            return ""
        icon_html = f'<div class="fs-callout-icon">{_html_text(icon)}</div>' if icon else ""
        return (
            f'<div class="{_html_attr(" ".join(classes))}">'
            f"{icon_html}<div class=\"fs-callout-content\">{content_html}</div>"
            f"</div>"
        )

    async def render_quote_container(self, block: dict[str, Any]) -> str:
        content_html = await self.render_children_as_html(block)
        if not content_html:
            return ""
        return f'<blockquote class="fs-quote-container">{content_html}</blockquote>'

    def _callout_icon(self, callout: dict[str, Any]) -> str:
        raw = callout.get("emoji") or callout.get("emoji_id") or callout.get("emojiId") or ""
        if isinstance(raw, dict):
            raw = raw.get("emoji") or raw.get("unicode") or raw.get("id") or ""
        raw = str(raw or "").strip()
        if not raw:
            return ""
        if len(raw) <= 4 and not raw.isascii():
            return raw
        return "💡"

    async def render_block_html(self, block: dict[str, Any]) -> str:
        block_type = _block_type(block)
        if block_type == 22 or "divider" in block:
            return "<hr>"
        if block_type == 19 or "callout" in block:
            return await self.render_callout(block)
        if block_type == 34 or "quote_container" in block:
            return await self.render_quote_container(block)
        if block_type == 27 or "image" in block:
            url = await self.upload_image(block)
            return f'<p><img src="{_html_attr(url)}" alt="图片"></p>' if url else ""
        if block_type == 31 or "table" in block:
            table_md = await self.render_table(block)
            return f"<pre>{_html_text(table_md)}</pre>" if table_md else ""
        field_name = self._text_field_name(block)
        if field_name:
            rendered_text = self.render_text_like_html(block, field_name)
            rendered_children = await self.render_children_as_html(block)
            return self.join_rendered_parts(rendered_text, rendered_children)
        return await self.render_children_as_html(block)

    async def render_table(self, block: dict[str, Any]) -> str:
        table = block.get("table") or {}
        props = table.get("property") or table.get("properties") or {}
        column_size = (
            table.get("column_size")
            or table.get("column_count")
            or props.get("column_size")
            or props.get("column_count")
            or props.get("column")
        )
        try:
            col_count = int(column_size)
        except (TypeError, ValueError):
            col_count = 0
        cell_ids = _child_ids(block)
        if not col_count or not cell_ids:
            self.warn("发现表格但缺少行列信息，已按普通文本导入")
            return await self.render_children_as_text(block)

        cells: list[str] = []
        for cell_id in cell_ids:
            cell = self.blocks_by_id.get(cell_id)
            if not cell:
                cells.append("")
                continue
            cell_text = await self.render_children_as_text(cell, compact=True)
            cells.append(_escape_table_cell(cell_text))
        rows = [cells[i:i + col_count] for i in range(0, len(cells), col_count)]
        if not rows:
            return ""
        rows = [row + [""] * (col_count - len(row)) for row in rows]
        header = rows[0]
        body = rows[1:] or [[""] * col_count]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * col_count) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n".join(lines)

    async def render_children_as_text(self, block: dict[str, Any], *, compact: bool = False) -> str:
        parts: list[str] = []
        for child_id in _child_ids(block):
            child = self.blocks_by_id.get(child_id)
            if not child:
                continue
            rendered = await self.render_block(child, compact=compact)
            if rendered.strip():
                parts.append(rendered.strip())
        return (" " if compact else "\n").join(parts)

    async def render_children_as_html(self, block: dict[str, Any]) -> str:
        parts: list[str] = []
        for child_id in _child_ids(block):
            child = self.blocks_by_id.get(child_id)
            if not child:
                continue
            rendered = await self.render_block_html(child)
            if rendered.strip():
                parts.append(rendered.strip())
        return "\n".join(parts)


async def import_feishu_docx(url: str, *, user_id: int) -> FeishuImportedDocument:
    document_id = parse_feishu_docx_id(url)
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=20.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        token = await _tenant_access_token(client)
        info = await _get_document_info(client, token, document_id)
        blocks = await _list_document_blocks(client, token, document_id)
        renderer = FeishuMarkdownRenderer(
            client=client,
            token=token,
            document_id=document_id,
            blocks=blocks,
            user_id=user_id,
        )
        markdown = await renderer.render()
        title = _extract_document_title(info) or renderer.first_heading or "飞书导入内容"
        return FeishuImportedDocument(
            title=title.strip()[:100],
            markdown=markdown,
            image_count=renderer.image_count,
            style_count=renderer.style_count,
            callout_count=renderer.callout_count,
            warnings=renderer.warnings,
        )
