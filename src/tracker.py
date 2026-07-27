#!/usr/bin/env python3
"""
arxiv + DBLP daily tracker.

For each topic in keywords.yaml:
  1. Query arxiv (free, keyless) for recent papers matching the configured queries.
  2. Query DBLP (free, keyless) for TCAD/TODAES/DAC/ICCAD/DATE papers.
  3. Dedupe against state.json.
  4. For each new paper, ask the OpenAI-compatible LLM for a structured summary.
  5. Append to a per-day markdown digest.

Usage:
    python tracker.py                    # run for all topics, today
    python tracker.py --dry-run          # fetch only, no LLM calls
    python tracker.py --topic ml_for_eda # only one topic
    python tracker.py --since-days 7     # override days_back

Environment (read from os.environ, never written to disk):
    OPENAI_API_KEY   : key for LLM
    OPENAI_BASE_URL  : OpenAI-compatible endpoint
    OPENAI_MODEL     : model name (default: gpt-4o-mini)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "keywords.yaml"
STATE_PATH = ROOT / "state.json"
PAPERS_DIR = ROOT / "papers"
DIGESTS_DIR = ROOT / "digests"

ARXIV_ENDPOINT = "http://export.arxiv.org/api/query"
DBLP_ENDPOINT = "https://dblp.org/search/publ/api"

ATOM_NS = "{http://www.w3.org/2005/Atom}"


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #

@dataclass
class Paper:
    source: str            # "arxiv" | "dblp"
    paper_id: str          # arxiv id or dblp key
    title: str
    authors: list[str]
    abstract: str
    url: str
    venue: str = ""        # "arXiv:cs.AR" or "IEEE Trans. CAD (2026)"
    pdf_url: str = ""
    published: str = ""    # ISO date
    topic: str = ""

    @property
    def dedup_key(self) -> str:
        raw = f"{self.source}:{self.paper_id}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #

def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"seen": {}, "last_run": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# arxiv
# --------------------------------------------------------------------------- #

def query_arxiv(query: str, days_back: int, max_results: int = 50) -> list[Paper]:
    cutoff = dt.date.today() - dt.timedelta(days=days_back)
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    # arxiv asks us to set a User-Agent
    headers = {"User-Agent": "research-workflow-tracker/1.0 (mailto:none@example.com)"}
    try:
        r = requests.get(ARXIV_ENDPOINT, params=params, headers=headers, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[arxiv] query failed: {query!r}: {e}", file=sys.stderr)
        return []

    papers: list[Paper] = []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"[arxiv] xml parse error: {e}", file=sys.stderr)
        return []

    for entry in root.findall(f"{ATOM_NS}entry"):
        title_el = entry.find(f"{ATOM_NS}title")
        summary_el = entry.find(f"{ATOM_NS}summary")
        published_el = entry.find(f"{ATOM_NS}published")
        id_el = entry.find(f"{ATOM_NS}id")
        if title_el is None or id_el is None:
            continue
        raw_id = id_el.text.strip().rstrip("/")
        arxiv_id = raw_id.split("/abs/")[-1]
        published = (published_el.text if published_el is not None else "")[:10]
        try:
            pub_date = dt.date.fromisoformat(published)
        except ValueError:
            pub_date = dt.date.today()
        if pub_date < cutoff:
            continue

        link_pdf = ""
        for link in entry.findall(f"{ATOM_NS}link"):
            if link.get("title") == "pdf":
                link_pdf = link.get("href", "")
                break

        authors = [a.find(f"{ATOM_NS}name").text
                   for a in entry.findall(f"{ATOM_NS}author")
                   if a.find(f"{ATOM_NS}name") is not None]

        # figure out primary category
        primary = entry.find("{http://arxiv.org/schemas/atom}primary_category")
        cat = primary.get("term") if primary is not None else "arxiv"

        papers.append(Paper(
            source="arxiv",
            paper_id=arxiv_id,
            title=title_el.text.strip().replace("\n", " "),
            authors=authors,
            abstract=(summary_el.text.strip() if summary_el is not None else "").replace("\n", " "),
            url=f"https://arxiv.org/abs/{arxiv_id}",
            venue=f"arXiv:{cat}",
            pdf_url=link_pdf,
            published=published,
        ))
    return papers


# --------------------------------------------------------------------------- #
# DBLP
# --------------------------------------------------------------------------- #

VENUE_KEYWORDS = {
    "tcad":   ["tcad", "ieee trans. comput.-aided design", "ieee transactions on computer-aided design"],
    "todaes": ["todaes", "acm trans. des. autom. electron. syst."],
    "dac":    ["dac", "design automation conference"],
    "iccad":  ["iccad", "int. conf. computer-aided design"],
    "date":   ["date", "design, automation & test in europe"],
}


def _venue_match(venue_str: str, allowed: list[str]) -> bool:
    v = (venue_str or "").lower()
    for needed in allowed:
        for kw in VENUE_KEYWORDS.get(needed.lower(), [needed.lower()]):
            if kw in v:
                return True
    return False


_DBLP_MIN_INTERVAL_SEC = 31.0
_dblp_last_call_ts: float = 0.0


def query_dblp(query: str, venues: list[str], year: int) -> list[Paper]:
    global _dblp_last_call_ts
    now = time.time()
    wait = _DBLP_MIN_INTERVAL_SEC - (now - _dblp_last_call_ts)
    if wait > 0:
        print(f"[dblp] sleeping {wait:.0f}s before next query (rate limit)...", flush=True)
        time.sleep(wait)
    _dblp_last_call_ts = time.time()

    params = {
        "q": query,
        "format": "json",
        "h": 50,
        "f": 0,
    }
    headers = {"User-Agent": "research-workflow-tracker/1.0 (mailto:none@example.com)"}
    try:
        r = requests.get(DBLP_ENDPOINT, params=params, headers=headers, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[dblp] query failed: {query!r}: {e}", file=sys.stderr)
        return []

    hits = r.json().get("result", {}).get("hits", {}).get("hit", [])
    papers: list[Paper] = []
    for hit in hits:
        info = hit.get("info", {})
        venue = info.get("venue", "")
        if not _venue_match(venue, venues):
            continue
        try:
            y = int(info.get("year", "0"))
        except ValueError:
            y = 0
        if y < year:
            continue
        title = info.get("title", "").strip().rstrip(".")
        dblp_key = hit.get("@id") or info.get("key", "")
        url = info.get("url") or info.get("ee") or f"https://dblp.org/rec/{dblp_key}"
        authors_raw = info.get("authors", {}).get("author", [])
        if isinstance(authors_raw, dict):
            authors_raw = [authors_raw]
        authors = [a.get("text", "") for a in authors_raw if isinstance(a, dict)]

        papers.append(Paper(
            source="dblp",
            paper_id=dblp_key,
            title=title,
            authors=authors,
            abstract="",  # DBLP has no abstract
            url=url,
            venue=f"{venue} ({y})",
            pdf_url="",
            published=f"{y}-01-01",
        ))
    return papers


# --------------------------------------------------------------------------- #
# LLM summary
# --------------------------------------------------------------------------- #

def make_llm_client():
    from openai import OpenAI
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return OpenAI(base_url=base_url, api_key=api_key)


def summarize_paper(paper: Paper, language: str = "zh") -> dict[str, str]:
    """Ask the LLM for a structured summary. Returns dict with motivation/method/relevance/limitation."""
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    client = make_llm_client()
    abstract = paper.abstract or "(no abstract available — DBLP metadata only)"
    lang_note = "用中文输出" if language == "zh" else "Output in English."
    system = (
        "You are a research assistant that produces a faithful, concise summary of an academic paper. "
        "Do not hallucinate facts not supported by the abstract. If the abstract is missing, say so explicitly."
    )
    user = (
        f"Title: {paper.title}\n"
        f"Authors: {', '.join(paper.authors[:5])}\n"
        f"Venue: {paper.venue}\n"
        f"Abstract: {abstract}\n\n"
        f"Produce a JSON object with keys: motivation, method, key_result, "
        f"relevance_to_topic, limitations. Each value should be 1-2 sentences. {lang_note}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=600,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # model occasionally wraps JSON in code fences
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
        return {"relevance_to_topic": raw[:200]}


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _download_pdf(paper: Paper) -> None:
    if not paper.pdf_url:
        return
    out = PAPERS_DIR / f"{paper.source}-{paper.paper_id.replace('/', '_')}.pdf"
    if out.exists() and out.stat().st_size > 0:
        return
    try:
        r = requests.get(paper.pdf_url, timeout=60)
        r.raise_for_status()
        out.write_bytes(r.content)
        paper.pdf_url_local = str(out.relative_to(ROOT))
    except Exception as e:
        print(f"[pdf] download failed {paper.paper_id}: {e}", file=sys.stderr)


def collect_papers(config: dict, state: dict, since_days: int, topic_filter: str | None,
                   no_arxiv: bool = False, no_dblp: bool = False) -> dict[str, list[Paper]]:
    year_floor = dt.date.today().year - 1
    result: dict[str, list[Paper]] = {}
    seen_titles = set(state.get("seen_titles", []))

    for topic, cfg in config.get("topics", {}).items():
        if topic_filter and topic != topic_filter:
            continue
        bucket: list[Paper] = []
        if not no_arxiv:
            for q in cfg.get("arxiv_queries", []):
                for p in query_arxiv(q, days_back=since_days):
                    p.topic = topic
                    bucket.append(p)
        if not no_dblp:
            for q in cfg.get("dblp_queries", []):
                for p in query_dblp(q, venues=cfg.get("dblp_venues", []), year=year_floor):
                    p.topic = topic
                    bucket.append(p)

        # dedupe by paper_id, by URL, and by normalized title
        unique: list[Paper] = []
        local_seen: set[str] = set()
        for p in bucket:
            key = p.dedup_key
            title_key = _norm(p.title)
            if key in local_seen or title_key in seen_titles:
                continue
            local_seen.add(key)
            seen_titles.add(title_key)
            unique.append(p)
        result[topic] = unique[: config.get("max_per_topic", 25)]
    state["seen_titles"] = list(seen_titles)[-5000:]  # bound memory
    return result


def write_digest(today: str, by_topic: dict[str, list[Paper]], summaries: dict[str, dict], config: dict) -> Path:
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    out = DIGESTS_DIR / f"{today}.md"
    lines = [f"# 每日论文摘要 — {today}", ""]
    lang = config.get("summary_language", "zh")
    descr = {
        k: v.get("description", "") for k, v in config.get("topics", {}).items()
    }
    for topic, papers in by_topic.items():
        if not papers:
            continue
        lines.append(f"## {topic} — {descr.get(topic, '')} ({len(papers)} 篇)")
        lines.append("")
        for p in papers:
            lines.append(f"### {p.title}")
            authors = ", ".join(p.authors[:5]) + (" et al." if len(p.authors) > 5 else "")
            lines.append(f"- **作者**: {authors}")
            lines.append(f"- **来源**: {p.venue}  · `{p.paper_id}`")
            lines.append(f"- **链接**: {p.url}")
            if p.pdf_url:
                lines.append(f"- **PDF**: {p.pdf_url}")
            lines.append(f"- **发布日期**: {p.published}")
            s = summaries.get(p.dedup_key, {})
            if s:
                if lang == "zh":
                    lines.append("")
                    lines.append(f"> **动机**: {s.get('motivation', '')}")
                    lines.append(f"> **方法**: {s.get('method', '')}")
                    lines.append(f"> **关键结果**: {s.get('key_result', '')}")
                    lines.append(f"> **与本课题相关性**: {s.get('relevance_to_topic', '')}")
                    lines.append(f"> **局限**: {s.get('limitations', '')}")
                else:
                    for k_, v_ in s.items():
                        lines.append(f"- **{k_}**: {v_}")
            elif p.abstract:
                lines.append("")
                lines.append(f"> {p.abstract[:400]}{'...' if len(p.abstract) > 400 else ''}")
            lines.append("")
        lines.append("")
    out.write_text("\n".join(lines))
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch only, no LLM call")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--since-days", type=int, default=None)
    ap.add_argument("--no-pdf", action="store_true", help="skip PDF download")
    ap.add_argument("--no-dblp", action="store_true", help="skip DBLP queries (useful if rate-limited)")
    ap.add_argument("--no-arxiv", action="store_true", help="skip arxiv queries")
    args = ap.parse_args(argv)

    config = load_config()
    since_days = args.since_days if args.since_days else int(config.get("days_back", 14))
    state = load_state()

    print(f"[tracker] collecting papers from past {since_days} days...", flush=True)
    by_topic = collect_papers(config, state, since_days=since_days, topic_filter=args.topic,
                              no_arxiv=args.no_arxiv, no_dblp=args.no_dblp)
    save_state(state)

    total = sum(len(v) for v in by_topic.values())
    print(f"[tracker] collected {total} new papers across {len(by_topic)} topics", flush=True)
    if total == 0:
        print("[tracker] nothing new today.")
        return 0

    summaries: dict[str, dict] = {}
    if not args.dry_run:
        print("[tracker] generating LLM summaries...", flush=True)
        for topic, papers in by_topic.items():
            for i, p in enumerate(papers, 1):
                print(f"  [{topic}] {i}/{len(papers)} {p.paper_id}", flush=True)
                try:
                    summaries[p.dedup_key] = summarize_paper(p, language=config.get("summary_language", "zh"))
                except Exception as e:
                    print(f"    LLM failed: {e}", file=sys.stderr)
                    summaries[p.dedup_key] = {"relevance_to_topic": f"(summary failed: {e})"}
                time.sleep(0.3)
        if not args.no_pdf:
            for papers in by_topic.values():
                for p in papers:
                    if p.source == "arxiv":
                        _download_pdf(p)

    today = dt.date.today().isoformat()
    out = write_digest(today, by_topic, summaries, config)
    print(f"[tracker] digest written -> {out.relative_to(ROOT)}", flush=True)
    state["last_run"] = today
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
