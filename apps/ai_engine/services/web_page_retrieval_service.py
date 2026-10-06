import html
import ipaddress
import json
import os
import re
import socket

from html.parser import HTMLParser
from urllib.parse import (
    urljoin,
    urlsplit,
)

import requests


class _ReadableHTMLParser(HTMLParser):
    SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "nav",
        "footer",
        "form",
        "button",
    }

    TEXT_TAGS = {
        "h1",
        "h2",
        "h3",
        "p",
        "li",
        "blockquote",
    }

    def __init__(self):
        super().__init__(
            convert_charrefs=True,
        )

        self.skip_depth = 0

        self.current_text_tag = None

        self.current_text = []
        self.blocks = []

        self.title = ""
        self.in_title = False
        self.title_parts = []

        self.published_at = ""

    def handle_starttag(
        self,
        tag,
        attrs,
    ):
        tag = tag.lower()

        attrs_dict = {str(key).lower(): value for key, value in attrs}

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return

        if self.skip_depth:
            return

        if tag == "title":
            self.in_title = True
            self.title_parts = []
            return

        if tag == "meta":
            key = str(
                attrs_dict.get("property") or attrs_dict.get("name") or ""
            ).lower()

            content = str(attrs_dict.get("content") or "").strip()

            if (
                key
                in {
                    "article:published_time",
                    "date",
                    "datepublished",
                    "publishdate",
                    "pubdate",
                }
                and content
                and not self.published_at
            ):
                self.published_at = content[:120]

            return

        if tag in self.TEXT_TAGS:
            self.current_text_tag = tag
            self.current_text = []

    def handle_endtag(
        self,
        tag,
    ):
        tag = tag.lower()

        if tag in self.SKIP_TAGS:
            self.skip_depth = max(
                0,
                self.skip_depth - 1,
            )
            return

        if self.skip_depth:
            return

        if tag == "title" and self.in_title:
            self.title = self._clean(" ".join(self.title_parts))[:300]

            self.in_title = False
            self.title_parts = []
            return

        if self.current_text_tag == tag:
            value = self._clean(" ".join(self.current_text))

            if len(value) >= 30:
                self.blocks.append(value)

            self.current_text_tag = None

            self.current_text = []

    def handle_data(
        self,
        data,
    ):
        if self.skip_depth:
            return

        value = str(data or "").strip()

        if not value:
            return

        if self.in_title:
            self.title_parts.append(value)

        if self.current_text_tag:
            self.current_text.append(value)

    @staticmethod
    def _clean(
        value,
    ):
        return re.sub(
            r"\s+",
            " ",
            html.unescape(str(value or "")),
        ).strip()


class WebPageRetrievalService:
    """
    Fetch publisher pages through a bounded,
    SSRF-aware boundary.

    Infrastructure should ALSO block RFC1918,
    link-local and cloud metadata egress.
    """

    USER_AGENT = "Mozilla/5.0 " "(compatible; " "DocuXWebRetriever/1.0)"

    @classmethod
    def enrich_results(
        cls,
        results,
        max_results=4,
    ):
        output = []
        warnings = []

        for result in list(results or []):
            enriched = dict(result)

            if len(output) < max_results and enriched.get("url"):
                try:
                    page = cls.fetch(enriched["url"])

                    if page:
                        enriched.update(page)

                except Exception as exc:
                    warnings.append(
                        (
                            "Unable to read "
                            f"{enriched.get('url')}: "
                            f"{type(exc).__name__}"
                        )
                    )

            output.append(enriched)

        return {
            "results": output,
            "warnings": warnings,
        }

    @classmethod
    def fetch(
        cls,
        url,
    ):
        current_url = str(url or "").strip()

        if not current_url:
            return None

        timeout = float(
            os.getenv(
                "WEB_PAGE_FETCH_TIMEOUT_SECONDS",
                "10",
            )
        )

        max_bytes = int(
            os.getenv(
                "WEB_PAGE_FETCH_MAX_BYTES",
                "1500000",
            )
        )

        max_chars = int(
            os.getenv(
                "WEB_PAGE_FETCH_MAX_CHARS",
                "14000",
            )
        )

        max_redirects = int(
            os.getenv(
                "WEB_PAGE_FETCH_MAX_REDIRECTS",
                "4",
            )
        )

        session = requests.Session()

        for _ in range(max_redirects + 1):
            cls.validate_public_url(current_url)

            response = session.get(
                current_url,
                headers={
                    "User-Agent": cls.USER_AGENT,
                    "Accept": ("text/html," "text/plain;q=0.9," "*/*;q=0.1"),
                },
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")

                response.close()

                if not location:
                    return None

                current_url = urljoin(
                    current_url,
                    location,
                )

                continue

            response.raise_for_status()

            content_type = str(response.headers.get("Content-Type") or "").lower()

            if not any(
                allowed in content_type
                for allowed in (
                    "text/html",
                    "application/xhtml+xml",
                    "text/plain",
                )
            ):
                response.close()
                return None

            body = bytearray()

            for chunk in response.iter_content(
                chunk_size=16384,
            ):
                if not chunk:
                    continue

                body.extend(chunk)

                if len(body) > max_bytes:
                    break

            encoding = response.encoding or "utf-8"

            response.close()

            text = bytes(body[:max_bytes]).decode(
                encoding,
                errors="replace",
            )

            if "text/plain" in content_type:
                content = re.sub(
                    r"\s+",
                    " ",
                    text,
                ).strip()[:max_chars]

                if not content:
                    return None

                return {
                    "url": current_url,
                    "content": content,
                    "content_source": "publisher_page",
                    "published_at": "",
                }

            parsed = cls.extract_html(
                text,
                max_chars=max_chars,
            )

            if not parsed.get("content"):
                return None

            return {
                "url": current_url,
                "title": (parsed.get("title") or None),
                "content": parsed["content"],
                "content_source": "publisher_page",
                "published_at": (parsed.get("published_at") or ""),
            }

        return None

    @classmethod
    def validate_public_url(
        cls,
        url,
    ):
        parsed = urlsplit(str(url or ""))

        if parsed.scheme not in {
            "http",
            "https",
        }:
            raise ValueError("Only HTTP(S) URLs " "may be fetched.")

        hostname = (parsed.hostname or "").strip().lower().rstrip(".")

        if not hostname:
            raise ValueError("URL hostname is required.")

        if hostname in {
            "localhost",
            "localhost.localdomain",
        } or hostname.endswith(
            (
                ".local",
                ".internal",
                ".localhost",
            )
        ):
            raise ValueError("Local/internal hosts " "are not allowed.")

        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        addresses = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )

        if not addresses:
            raise ValueError("Hostname did not resolve.")

        for item in addresses:
            raw_ip = item[4][0]

            ip = ipaddress.ip_address(raw_ip)

            if not ip.is_global:
                raise ValueError("Non-public destination " "is not allowed.")

        return True

    @classmethod
    def extract_html(
        cls,
        raw_html,
        max_chars=14000,
    ):
        parser = _ReadableHTMLParser()

        parser.feed(str(raw_html or ""))

        blocks = []
        seen = set()

        for block in parser.blocks:
            normalized = re.sub(
                r"\s+",
                " ",
                block,
            ).strip()

            key = normalized.lower()

            if not normalized or key in seen:
                continue

            seen.add(key)

            blocks.append(normalized)

        content = "\n\n".join(blocks)[:max_chars].strip()

        if not parser.published_at:
            parser.published_at = cls._published_from_json_ld(raw_html)

        return {
            "title": parser.title,
            "content": content,
            "published_at": parser.published_at,
        }

    @staticmethod
    def _published_from_json_ld(
        raw_html,
    ):
        matches = re.findall(
            (
                r"<script[^>]+"
                r'type=["\']'
                r"application/ld\+json"
                r'["\'][^>]*>'
                r"(.*?)"
                r"</script>"
            ),
            str(raw_html or ""),
            flags=(re.I | re.S),
        )

        for match in matches:
            try:
                payload = json.loads(html.unescape(match).strip())

            except Exception:
                continue

            candidates = (
                payload
                if isinstance(
                    payload,
                    list,
                )
                else [payload]
            )

            for candidate in candidates:
                if not isinstance(
                    candidate,
                    dict,
                ):
                    continue

                value = candidate.get("datePublished") or candidate.get("dateModified")

                if value:
                    return str(value)[:120]

        return ""
