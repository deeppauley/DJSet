import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import slskd_api


DEFAULT_URL = "http://localhost:5030"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_EMPTY_RESPONSE_ABORT_SECONDS = 3.0
LAST_SEARCH_FILE = Path(__file__).with_name(".last_soulseek_search.json")
DEFAULT_TRACKS_FILE = Path(__file__).with_name("tracks.txt")
DEFAULT_BATCH_REPORT_FILE = Path(__file__).with_name("soulseek_batch_report.json")
SUPPORTED_EXTENSIONS = ["flac", "mp3", "wav", "aiff", "aif", "m4a"]
STOP_WORDS = {
    "and",
    "feat",
    "featuring",
    "ft",
    "original",
    "extended",
    "clean",
    "dirty",
    "radio",
    "edit",
    "mix",
    "remix",
    "with",
    "adjacent",
    "beach",
    "club",
    "deep",
    "disco",
    "festival",
    "house",
    "melodic",
    "modern",
    "nu",
    "sax",
    "smooth",
    "sunset",
    "tropical",
    "version",
    "yacht",
}


class SearchCancelled(Exception):
    pass


def get_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def require_config() -> tuple[str, str]:
    url = os.environ.get("SLSKD_URL", DEFAULT_URL)
    api_key = os.environ.get("SLSKD_API_KEY")
    if not api_key:
        raise SystemExit(
            "Missing SLSKD_API_KEY. Set it first, for example:\n"
            '$env:SLSKD_API_KEY="your-slskd-api-key"'
        )
    return url, api_key


def make_client() -> slskd_api.SlskdClient:
    url, api_key = require_config()
    return slskd_api.SlskdClient(url, api_key=api_key, timeout=30)


def bytes_label(value: int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def speed_label(value: int | float | None) -> str:
    if not value:
        return "?"
    return f"{bytes_label(int(value))}/s"


def seconds_label(value: int | None) -> str:
    if not value:
        return "?"
    minutes, seconds = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def text_tokens(value: str) -> set[str]:
    return {token for token in normalize_text(value).split() if token not in STOP_WORDS and len(token) > 1}


def remix_tokens(query: str) -> set[str]:
    tokens: set[str] = set()
    for parenthetical in re.findall(r"\(([^)]*remix[^)]*)\)", query, flags=re.IGNORECASE):
        tokens |= text_tokens(parenthetical)
    return tokens


def remix_text(query: str) -> str:
    matches = re.findall(r"\(([^)]*remix[^)]*)\)", query, flags=re.IGNORECASE)
    return " ".join(matches)


def query_variants(query: str) -> list[str]:
    variants = [query]
    without_parentheticals = re.sub(r"\([^)]*\)", " ", query)
    without_parentheticals = re.sub(r"\s+", " ", without_parentheticals).strip()

    remix = remix_text(query)
    if remix:
        base_tokens = [token for token in normalize_text(without_parentheticals).split() if token not in STOP_WORDS]
        remix_query_tokens = [token for token in normalize_text(remix).split() if token not in STOP_WORDS]
        title_tail = base_tokens[-4:]
        title_short_tail = base_tokens[-2:]
        if title_tail and remix_query_tokens:
            variants.append(" ".join(title_tail + remix_query_tokens + ["remix"]))
        if title_short_tail and remix_query_tokens:
            variants.append(" ".join(title_short_tail + remix_query_tokens))
    else:
        base_tokens = [token for token in normalize_text(query).split() if token not in STOP_WORDS]
        if base_tokens:
            variants.append(" ".join(base_tokens))

    unique_variants = []
    seen = set()
    for variant in variants:
        normalized = normalize_text(variant)
        if normalized and normalized not in seen:
            unique_variants.append(variant)
            seen.add(normalized)
    return unique_variants


def result_filename_text(result: dict[str, Any]) -> str:
    return normalize_text(result_basename(result))


def result_basename(result: dict[str, Any]) -> str:
    filename = result.get("filename", "")
    return re.split(r"[\\/]", filename)[-1]


def is_320_mp3(result: dict[str, Any]) -> bool:
    filename = result.get("filename", "").lower()
    extension = (result.get("extension") or Path(filename).suffix.lstrip(".")).lower()
    return extension == "mp3" and (result.get("bitRate") or 0) >= 320


def is_flac(result: dict[str, Any]) -> bool:
    filename = result.get("filename", "").lower()
    extension = (result.get("extension") or Path(filename).suffix.lstrip(".")).lower()
    return extension == "flac" or filename.endswith(".flac")


def quality_rank(result: dict[str, Any]) -> int:
    if is_320_mp3(result):
        return 0
    if is_flac(result):
        return 1
    if (result.get("extension") or "").lower() == "mp3":
        return 2
    return 3


def serialize_result(username: str, file_item: Any, response: Any) -> dict[str, Any]:
    return {
        "username": username,
        "filename": get_value(file_item, "filename", ""),
        "size": get_value(file_item, "size"),
        "extension": get_value(file_item, "extension"),
        "bitRate": get_value(file_item, "bitRate"),
        "length": get_value(file_item, "length"),
        "isLocked": get_value(file_item, "isLocked", False),
        "hasFreeUploadSlot": get_value(response, "hasFreeUploadSlot", False),
        "queueLength": get_value(response, "queueLength"),
        "uploadSpeed": get_value(response, "uploadSpeed"),
        "file": dict(file_item),
    }


def result_sort_key(result: dict[str, Any], query: str = "") -> tuple[Any, ...]:
    required_remix_tokens = remix_tokens(query)
    filename_tokens = text_tokens(result_basename(result))
    remix_miss = 0
    if required_remix_tokens:
        remix_miss = 0 if required_remix_tokens <= filename_tokens else 1

    return (
        remix_miss,
        quality_rank(result),
        result.get("queueLength") if result.get("queueLength") is not None else 999999,
        -(result.get("length") or 0),
        not result.get("hasFreeUploadSlot", False),
        result.get("isLocked", False),
        -(result.get("uploadSpeed") or 0),
    )


def print_result(index: int, result: dict[str, Any]) -> None:
    bitrate = result.get("bitRate")
    bitrate_text = f"{bitrate} kbps" if bitrate else "?"
    print(
        f"[{index:02d}] {result['username']} | "
        f"slot={'yes' if result.get('hasFreeUploadSlot') else 'no'} | "
        f"queue={result.get('queueLength', '?')} | "
        f"speed={speed_label(result.get('uploadSpeed'))} | "
        f"{bitrate_text} | {seconds_label(result.get('length'))} | "
        f"{bytes_label(result.get('size'))}"
    )
    print(f"     {result['filename']}")


def relevance_score(result: dict[str, Any], query: str) -> float:
    query_tokens = text_tokens(query)
    filename_tokens = text_tokens(result_basename(result))
    if not query_tokens:
        return 0
    return len(query_tokens & filename_tokens) / len(query_tokens)


def is_relevant_result(result: dict[str, Any], query: str) -> bool:
    query_tokens = text_tokens(query)
    filename_tokens = text_tokens(result_basename(result))
    required_remix_tokens = remix_tokens(query)
    if required_remix_tokens and not required_remix_tokens <= filename_tokens:
        return False
    if len(query_tokens) <= 2:
        return len(query_tokens & filename_tokens) == len(query_tokens)
    return relevance_score(result, query) >= 0.45 and len(query_tokens & filename_tokens) >= 2


def search_results(
    client: slskd_api.SlskdClient,
    args: argparse.Namespace,
    query: str,
    relevance_query: str | None = None,
    cancel_check=None,
) -> list[dict[str, Any]]:
    state = client.searches.search_text(
        query,
        responseLimit=args.responses,
        fileLimit=args.file_limit,
        searchTimeout=args.timeout * 1000,
        filterResponses=True,
        minimumPeerUploadSpeed=args.min_speed,
        maximumPeerQueueLength=args.max_queue,
    )
    search_id = get_value(state, "id")
    empty_response_abort_seconds = float(
        getattr(args, "empty_response_abort_seconds", DEFAULT_EMPTY_RESPONSE_ABORT_SECONDS)
    )
    started_at = time.time()
    deadline = time.time() + args.timeout + 5
    last_response_count = 0
    while time.time() < deadline:
        if cancel_check and cancel_check():
            client.searches.stop(search_id)
            raise SearchCancelled(query)
        state = client.searches.state(search_id)
        last_response_count = get_value(state, "responseCount", 0) or last_response_count
        if get_value(state, "isComplete", False) and last_response_count > 0:
            break
        if (
            empty_response_abort_seconds > 0
            and last_response_count == 0
            and (time.time() - started_at) >= empty_response_abort_seconds
        ):
            client.searches.stop(search_id)
            break
        time.sleep(0.5)

    if cancel_check and cancel_check():
        client.searches.stop(search_id)
        raise SearchCancelled(query)

    responses = client.searches.search_responses(search_id)
    results: list[dict[str, Any]] = []
    extensions = tuple(f".{ext.lower().lstrip('.')}" for ext in args.ext)
    for response in responses:
        username = get_value(response, "username", "")
        files = get_value(response, "files", []) or []
        for file_item in files:
            filename = get_value(file_item, "filename", "")
            if args.ext and not filename.lower().endswith(extensions):
                continue
            result = serialize_result(username, file_item, response)
            if relevance_query and not is_relevant_result(result, relevance_query):
                continue
            results.append(result)

    results.sort(key=lambda result: result_sort_key(result, query))
    return results


def command_status(_: argparse.Namespace) -> int:
    client = make_client()
    server = client.server.state()
    app = client.application.state()
    print(f"slskd: {get_value(get_value(app, 'version', {}), 'full', '?')}")
    print(f"server: {get_value(server, 'state', '?')}")
    print(f"connected: {get_value(server, 'isConnected', False)}")
    print(f"logged in: {get_value(server, 'isLoggedIn', False)}")
    return 0


def command_connect(_: argparse.Namespace) -> int:
    client = make_client()
    print("connected" if client.server.connect() else "connect request failed")
    return 0


def command_search(args: argparse.Namespace) -> int:
    client = make_client()
    results = search_results(client, args, args.query)
    results = results[: args.limit]
    LAST_SEARCH_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"Search: {args.query}")
    print(f"Results saved: {LAST_SEARCH_FILE}")
    for index, result in enumerate(results, start=1):
        print_result(index, result)
    return 0


def command_best_download(args: argparse.Namespace) -> int:
    client = make_client()
    best, used_query, results = find_best_result(client, args, args.query)
    if not best:
        print(f"No results: {args.query}")
        return 1

    LAST_SEARCH_FILE.write_text(json.dumps(results[: args.limit], indent=2), encoding="utf-8")
    print(f"Best result for: {used_query}")
    print_result(1, best)

    if args.dry_run:
        print("Dry run: not queued.")
        return 0

    ok = client.transfers.enqueue(best["username"], [best["file"]])
    print(f"queued: {best['username']} | {best['filename']}" if ok else "queue request failed")
    return 0 if ok else 1


def find_best_result(
    client: slskd_api.SlskdClient,
    args: argparse.Namespace,
    query: str,
    excluded: set[tuple[str, str]] | None = None,
    cancel_check=None,
) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    results = []
    used_query = query
    excluded = excluded or set()
    variants = [query] if args.no_broad else query_variants(query)
    for variant in variants:
        print(f"Trying: {variant}")
        results = search_results(client, args, variant, relevance_query=query, cancel_check=cancel_check)
        results = [
            result
            for result in results
            if (result.get("username", ""), result.get("filename", "")) not in excluded
        ]
        if results:
            used_query = variant
            break
        time.sleep(args.pause)

    if not results:
        return None, used_query, []

    return results[0], used_query, results


def command_batch_best_download(args: argparse.Namespace) -> int:
    client = make_client()
    tracks = [
        line.strip()
        for line in args.file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if args.start_index > 1:
        tracks = tracks[args.start_index - 1 :]
    if args.max_tracks:
        tracks = tracks[: args.max_tracks]

    report: list[dict[str, Any]] = []
    queued_count = 0
    missing_count = 0

    for index, query in enumerate(tracks, start=1):
        display_index = args.start_index + index - 1
        print(f"\n[{display_index}] [{index}/{len(tracks)}] {query}")
        best, used_query, results = find_best_result(client, args, query)
        if not best:
            missing_count += 1
            report.append({"query": query, "status": "missing", "usedQuery": used_query})
            print("No results.")
            continue

        print_result(1, best)
        item = {
            "query": query,
            "status": "dry-run" if args.dry_run else "queued",
            "usedQuery": used_query,
            "username": best["username"],
            "filename": best["filename"],
            "bitRate": best.get("bitRate"),
            "length": best.get("length"),
            "size": best.get("size"),
            "qualityRank": quality_rank(best),
        }

        if args.dry_run:
            print("Dry run: not queued.")
        else:
            ok = client.transfers.enqueue(best["username"], [best["file"]])
            if ok:
                queued_count += 1
                print(f"queued: {best['username']} | {best['filename']}")
            else:
                item["status"] = "queue_failed"
                print("queue request failed")

        report.append(item)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        time.sleep(args.pause)

    print(f"\nQueued: {queued_count}")
    print(f"Missing: {missing_count}")
    print(f"Report: {args.report}")
    return 0 if missing_count == 0 else 1


def load_saved_results() -> list[dict[str, Any]]:
    if not LAST_SEARCH_FILE.exists():
        raise SystemExit("No saved search results. Run `search` first.")
    return json.loads(LAST_SEARCH_FILE.read_text(encoding="utf-8"))


def command_download(args: argparse.Namespace) -> int:
    client = make_client()
    if args.index is not None:
        results = load_saved_results()
        if args.index < 1 or args.index > len(results):
            raise SystemExit(f"Index out of range. Choose 1-{len(results)}.")
        result = results[args.index - 1]
        username = result["username"]
        file_item = result["file"]
    else:
        if not args.username or not args.filename:
            raise SystemExit("Use either `download INDEX` or `download --username USER --filename PATH`.")
        username = args.username
        file_item = {"filename": args.filename, "size": 0, "code": 0, "isLocked": False, "extension": ""}

    ok = client.transfers.enqueue(username, [file_item])
    print(f"queued: {username} | {file_item['filename']}" if ok else "queue request failed")
    return 0 if ok else 1


def command_downloads(_: argparse.Namespace) -> int:
    client = make_client()
    transfers = client.transfers.get_all_downloads()
    count = 0
    for transfer in transfers:
        username = get_value(transfer, "username", "?")
        for directory in get_value(transfer, "directories", []) or []:
            for file_item in get_value(directory, "files", []) or []:
                count += 1
                print(
                    f"{username} | {get_value(file_item, 'state', '?')} | "
                    f"{get_value(file_item, 'percentComplete', 0):.1f}% | "
                    f"{bytes_label(get_value(file_item, 'bytesTransferred'))}/"
                    f"{bytes_label(get_value(file_item, 'size'))} | "
                    f"{get_value(file_item, 'filename', '')}"
                )
    if count == 0:
        print("No downloads.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Small CLI client for a local slskd/Soulseek server.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show slskd and Soulseek connection status.")
    status.set_defaults(func=command_status)

    connect = subparsers.add_parser("connect", help="Ask slskd to connect to Soulseek.")
    connect.set_defaults(func=command_connect)

    search = subparsers.add_parser("search", help="Search Soulseek through slskd.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20, help="Maximum files to print.")
    search.add_argument("--responses", type=int, default=50, help="Maximum peer responses to collect.")
    search.add_argument("--file-limit", type=int, default=10000)
    search.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Search timeout in seconds.")
    search.add_argument(
        "--empty-response-abort-seconds",
        type=float,
        default=DEFAULT_EMPTY_RESPONSE_ABORT_SECONDS,
        help="Abort a search early if it has produced zero responses after this many seconds.",
    )
    search.add_argument("--min-speed", type=int, default=0, help="Minimum peer upload speed in bytes/sec.")
    search.add_argument("--max-queue", type=int, default=1000000)
    search.add_argument("--ext", nargs="*", default=SUPPORTED_EXTENSIONS)
    search.set_defaults(func=command_search)

    best_download = subparsers.add_parser(
        "best-download",
        help="Search, rank using preferred quality rules, and queue the best result.",
    )
    best_download.add_argument("query")
    best_download.add_argument("--limit", type=int, default=20, help="Maximum ranked files to cache.")
    best_download.add_argument("--responses", type=int, default=50, help="Maximum peer responses to collect.")
    best_download.add_argument("--file-limit", type=int, default=10000)
    best_download.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Search timeout in seconds.")
    best_download.add_argument(
        "--empty-response-abort-seconds",
        type=float,
        default=DEFAULT_EMPTY_RESPONSE_ABORT_SECONDS,
        help="Abort a search early if it has produced zero responses after this many seconds.",
    )
    best_download.add_argument("--min-speed", type=int, default=0, help="Minimum peer upload speed in bytes/sec.")
    best_download.add_argument("--max-queue", type=int, default=1000000)
    best_download.add_argument("--ext", nargs="*", default=SUPPORTED_EXTENSIONS)
    best_download.add_argument("--dry-run", action="store_true", help="Show the best result without queueing it.")
    best_download.add_argument("--no-broad", action="store_true", help="Only search the query exactly as typed.")
    best_download.add_argument("--pause", type=float, default=2.0, help="Seconds to pause between broad query attempts.")
    best_download.set_defaults(func=command_best_download)

    batch_best_download = subparsers.add_parser(
        "batch-best-download",
        help="Run best-download for each non-empty line in a track list.",
    )
    batch_best_download.add_argument("--file", type=Path, default=DEFAULT_TRACKS_FILE)
    batch_best_download.add_argument("--report", type=Path, default=DEFAULT_BATCH_REPORT_FILE)
    batch_best_download.add_argument("--start-index", type=int, default=1)
    batch_best_download.add_argument("--max-tracks", type=int, default=0)
    batch_best_download.add_argument("--limit", type=int, default=20, help="Maximum ranked files to cache per track.")
    batch_best_download.add_argument("--responses", type=int, default=50, help="Maximum peer responses to collect.")
    batch_best_download.add_argument("--file-limit", type=int, default=10000)
    batch_best_download.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Search timeout in seconds.")
    batch_best_download.add_argument(
        "--empty-response-abort-seconds",
        type=float,
        default=DEFAULT_EMPTY_RESPONSE_ABORT_SECONDS,
        help="Abort a search early if it has produced zero responses after this many seconds.",
    )
    batch_best_download.add_argument("--min-speed", type=int, default=0, help="Minimum peer upload speed in bytes/sec.")
    batch_best_download.add_argument("--max-queue", type=int, default=1000000)
    batch_best_download.add_argument("--ext", nargs="*", default=SUPPORTED_EXTENSIONS)
    batch_best_download.add_argument("--dry-run", action="store_true", help="Show best results without queueing them.")
    batch_best_download.add_argument("--no-broad", action="store_true", help="Only search each query exactly as typed.")
    batch_best_download.add_argument("--pause", type=float, default=2.0, help="Seconds to pause between searches.")
    batch_best_download.set_defaults(func=command_batch_best_download)

    download = subparsers.add_parser("download", help="Queue a download from saved search results or explicit peer/path.")
    download.add_argument("index", type=int, nargs="?", help="Result index from the most recent search.")
    download.add_argument("--username")
    download.add_argument("--filename")
    download.set_defaults(func=command_download)

    downloads = subparsers.add_parser("downloads", help="List current downloads.")
    downloads.set_defaults(func=command_downloads)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
