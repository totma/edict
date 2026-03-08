#!/usr/bin/env bash
# 新闻聚合 + 深度搜索 + 推送 一体化脚本
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="$(cd "$WORKSPACE_ROOT/.." && pwd)"
DATA_DIR="$WORKSPACE_ROOT/data"
FETCH_SCRIPT="$SCRIPT_DIR/fetch_morning_news.py"
DEEP_SEARCH_SCRIPT="$WORKSPACE_ROOT/skills/codex-deep-search/scripts/search.sh"
OPENCLAW_ENV_FILE="${OPENCLAW_ENV_FILE:-$OPENCLAW_HOME/.env}"

FORCE_FETCH=0
NO_PUSH=0
CLI_DRY_RUN=0
CLI_PUSH_CHANNEL=0
CLI_PUSH_TARGET=0
CLI_DEEP_TIMEOUT=0
CLI_TOP_N=0
CLI_TRANSLATE=0

DRY_RUN=""
PUSH_CHANNEL=""
PUSH_TARGET=""
DEEP_TIMEOUT=""
TOP_N=""
TRANSLATE_TO_ZH=""
PROMPT_SUFFIX=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE_FETCH=1; shift ;;
    --no-push) NO_PUSH=1; shift ;;
    --dry-run) DRY_RUN=1; CLI_DRY_RUN=1; shift ;;
    --channel) PUSH_CHANNEL="$2"; CLI_PUSH_CHANNEL=1; shift 2 ;;
    --target) PUSH_TARGET="$2"; CLI_PUSH_TARGET=1; shift 2 ;;
    --timeout) DEEP_TIMEOUT="$2"; CLI_DEEP_TIMEOUT=1; shift 2 ;;
    --top-n) TOP_N="$2"; CLI_TOP_N=1; shift 2 ;;
    --no-translate) TRANSLATE_TO_ZH=0; CLI_TRANSLATE=1; shift ;;
    --prompt-suffix) PROMPT_SUFFIX="$2"; shift 2 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

if [[ -f "$OPENCLAW_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OPENCLAW_ENV_FILE"
  set +a
fi

if [[ "$CLI_DRY_RUN" != "1" ]]; then
  DRY_RUN="${NEWS_PUSH_DRY_RUN:-0}"
fi
if [[ "$CLI_PUSH_CHANNEL" != "1" ]]; then
  PUSH_CHANNEL="${NEWS_PUSH_CHANNEL:-feishu}"
fi
if [[ "$CLI_PUSH_TARGET" != "1" ]]; then
  PUSH_TARGET="${NEWS_PUSH_TARGET:-}"
fi
if [[ "$CLI_DEEP_TIMEOUT" != "1" ]]; then
  DEEP_TIMEOUT="${NEWS_DEEPSEARCH_TIMEOUT:-120}"
fi
if [[ "$CLI_TOP_N" != "1" ]]; then
  TOP_N="${NEWS_DEEPSEARCH_TOP_N:-2}"
fi
if [[ "$CLI_TRANSLATE" != "1" ]]; then
  TRANSLATE_TO_ZH="${NEWS_TRANSLATE_TO_ZH:-1}"
fi

DRY_RUN="${DRY_RUN:-0}"
PUSH_CHANNEL="${PUSH_CHANNEL:-feishu}"
PUSH_TARGET="${PUSH_TARGET:-}"
DEEP_TIMEOUT="${DEEP_TIMEOUT:-120}"
TOP_N="${TOP_N:-2}"
TRANSLATE_TO_ZH="${TRANSLATE_TO_ZH:-1}"

mkdir -p "$DATA_DIR"

FETCH_ARGS=()
[[ "$FORCE_FETCH" == "1" ]] && FETCH_ARGS+=(--force)
python3 "$FETCH_SCRIPT" "${FETCH_ARGS[@]}"

BRIEF_JSON="$DATA_DIR/morning_brief.json"
if [[ ! -f "$BRIEF_JSON" ]]; then
  echo "ERROR: 未找到新闻聚合结果: $BRIEF_JSON" >&2
  exit 1
fi

TODAY="$(date +%Y%m%d)"
TASK_NAME="morning-push-${TODAY}-$(date +%H%M%S)"
PROMPT_FILE="$DATA_DIR/morning_deep_prompt_${TODAY}.txt"
DEEP_OUTPUT="$DATA_DIR/morning_brief_deep_${TODAY}.md"
DEEP_LOG="$DATA_DIR/morning_brief_deep_${TODAY}.log"
MSG_FILE_GC="$DATA_DIR/morning_push_message_gc_${TODAY}.txt"
MSG_FILE_INTL="$DATA_DIR/morning_push_message_intl_${TODAY}.txt"

python3 - "$BRIEF_JSON" "$PROMPT_FILE" "$TOP_N" "$PROMPT_SUFFIX" <<'PY'
import json, pathlib, sys

brief_path = pathlib.Path(sys.argv[1])
prompt_path = pathlib.Path(sys.argv[2])
top_n = max(1, int(sys.argv[3]))
suffix = sys.argv[4].strip()
data = json.loads(brief_path.read_text(encoding='utf-8'))

lines = [
    "请基于以下今日新闻聚合进行深度核验与扩展，输出中文简报。",
    f"日期: {data.get('date', '')}",
    "",
    "要求：",
    "1) 核验新闻时效性与可信度，标注关键来源链接。",
    "2) 提炼 5-8 条最重要结论，覆盖政治/军事/经济/AI。",
    "3) 指出潜在误报或旧闻回流。",
    "4) 给出今日风险雷达（高/中/低）与关注建议。",
    "",
    "聚合新闻：",
]

for category, items in (data.get("categories") or {}).items():
    lines.append(f"【{category}】")
    for idx, item in enumerate((items or [])[:top_n], 1):
        title = (item or {}).get("title", "")
        source = (item or {}).get("source", "")
        link = (item or {}).get("link", "")
        lines.append(f"{idx}. {title} | {source} | {link}")
    lines.append("")

if suffix:
    lines.extend(["附加要求：", suffix, ""])

prompt_path.write_text("\n".join(lines), encoding="utf-8")
PY

DEEP_EXIT=0
if [[ -f "$DEEP_SEARCH_SCRIPT" ]]; then
  echo "[bind] run deep-search task=$TASK_NAME timeout=${DEEP_TIMEOUT}s"
  set +e
  bash "$DEEP_SEARCH_SCRIPT" \
    --prompt "$(cat "$PROMPT_FILE")" \
    --task-name "$TASK_NAME" \
    --output "$DEEP_OUTPUT" \
    --timeout "$DEEP_TIMEOUT" >"$DEEP_LOG" 2>&1
  DEEP_EXIT=$?
  set -e
  echo "[bind] deep-search exit=$DEEP_EXIT log=$DEEP_LOG"
else
  DEEP_EXIT=127
fi

python3 - "$BRIEF_JSON" "$MSG_FILE_GC" "$MSG_FILE_INTL" "$DEEP_OUTPUT" "$DEEP_EXIT" "$TOP_N" "$TRANSLATE_TO_ZH" <<'PY'
import json, pathlib, sys, re, urllib.parse, urllib.request

brief_path = pathlib.Path(sys.argv[1])
gc_msg_path = pathlib.Path(sys.argv[2])
intl_msg_path = pathlib.Path(sys.argv[3])
deep_output = pathlib.Path(sys.argv[4])
deep_exit = int(sys.argv[5])
top_n = max(1, int(sys.argv[6]))
translate_to_zh = str(sys.argv[7]).strip().lower() in ("1", "true", "yes", "on")

data = json.loads(brief_path.read_text(encoding='utf-8'))
cats = data.get("categories") or {}

zh_char_re = re.compile(r'[\u4e00-\u9fff]')
translation_cache = {}

def translate_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if not translate_to_zh or zh_char_re.search(text):
        return text
    if text in translation_cache:
        return translation_cache[text]
    try:
        q = urllib.parse.urlencode({
            "client": "gtx",
            "sl": "auto",
            "tl": "zh-CN",
            "dt": "t",
            "q": text[:600],
        })
        url = f"https://translate.googleapis.com/translate_a/single?{q}"
        with urllib.request.urlopen(url, timeout=8) as resp:
            payload = resp.read().decode("utf-8", errors="ignore")
        arr = json.loads(payload)
        translated = "".join(seg[0] for seg in (arr[0] or []) if seg and seg[0]).strip()
        if translated:
            translation_cache[text] = translated
            return translated
    except Exception:
        pass
    translation_cache[text] = text
    return text

gc_kw_zh = ["中国", "大陆", "内地", "北京", "上海", "深圳", "广州", "香港", "澳门", "台湾", "台北", "两岸", "大中华"]
gc_kw_en = [
    "china", "chinese", "beijing", "shanghai", "shenzhen", "guangzhou",
    "hong kong", "macau", "macao", "taiwan", "taipei", "greater china", "cross-strait"
]
gc_domains = [
    "xinhuanet.com", "people.com.cn", "china.org.cn", "cgtn.com", "globaltimes.cn",
    "scmp.com", "rthk.hk", "hk01.com", "mingpao.com", "zaobao.com", "cna.com.tw",
    "taipeitimes.com", "udn.com", "chinatimes.com"
]

def is_greater_china(item: dict) -> bool:
    title = (item.get("title") or "")
    summary = (item.get("summary") or "")
    source = (item.get("source") or "")
    link = (item.get("link") or "")
    text_zh = f"{title} {summary} {source}"
    text_en = text_zh.lower()
    link_l = link.lower()
    if any(k in text_zh for k in gc_kw_zh):
        return True
    if any(k in text_en for k in gc_kw_en):
        return True
    if any(d in link_l for d in gc_domains):
        return True
    return False

if deep_exit == 0:
    deep_status = "ok"
elif deep_exit == 124:
    deep_status = "timeout"
elif deep_exit == 127:
    deep_status = "missing-script"
else:
    deep_status = f"failed({deep_exit})"

gc_cats = {}
intl_cats = {}
for c, items in cats.items():
    gc_items = []
    intl_items = []
    for it in (items or []):
        if is_greater_china(it or {}):
            gc_items.append(it)
        else:
            intl_items.append(it)
    if gc_items:
        gc_cats[c] = gc_items
    if intl_items:
        intl_cats[c] = intl_items

def build_message(title: str, bucket: dict) -> str:
    total = sum(len(v or []) for v in bucket.values())
    lines = [
        title,
        f"日期: {data.get('date', '')}",
        f"总条数: {total}",
        "",
    ]
    if total == 0:
        lines.append("今日未检索到对应分区新闻。")
    for c, items in bucket.items():
        lines.append(f"{c}: {len(items or [])} 条")
        for head in (items or [])[:top_n]:
            title_cn = translate_text(head.get("title", ""))
            summary_cn = translate_text(head.get("summary", ""))
            source = head.get("source", "")
            link = head.get("link", "")
            lines.append(f"  - {title_cn}")
            if summary_cn and summary_cn != title_cn:
                lines.append(f"    摘要: {summary_cn[:100]}")
            if source:
                lines.append(f"    来源: {source}")
            if link:
                lines.append(f"    {link}")
    lines.extend(["", f"深度搜索状态: {deep_status}"])
    if deep_output.exists():
        lines.append(f"深度报告: {deep_output}")
    if translate_to_zh:
        lines.append("说明: 已自动翻译为中文。")
    return "\n".join(lines)[:3500]

gc_msg_path.write_text(build_message("【大中华区域新闻汇总】", gc_cats), encoding="utf-8")
intl_msg_path.write_text(build_message("【国际新闻汇总】", intl_cats), encoding="utf-8")
PY

if [[ "$NO_PUSH" == "1" ]]; then
  echo "[bind] 已生成消息，未发送（--no-push）: $MSG_FILE_GC"
  echo "[bind] 已生成消息，未发送（--no-push）: $MSG_FILE_INTL"
  exit 0
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[bind] dry-run 消息预览(大中华): $MSG_FILE_GC"
  sed -n '1,120p' "$MSG_FILE_GC"
  echo "[bind] dry-run 消息预览(国际): $MSG_FILE_INTL"
  sed -n '1,120p' "$MSG_FILE_INTL"
  exit 0
fi

if [[ -z "$PUSH_TARGET" ]]; then
  echo "ERROR: 未设置推送目标。请设置 NEWS_PUSH_TARGET 或使用 --target" >&2
  echo "提示: 可先加 --dry-run 验证绑定链路。" >&2
  exit 2
fi

openclaw message send \
  --channel "$PUSH_CHANNEL" \
  --target "$PUSH_TARGET" \
  --message "$(cat "$MSG_FILE_GC")"

openclaw message send \
  --channel "$PUSH_CHANNEL" \
  --target "$PUSH_TARGET" \
  --message "$(cat "$MSG_FILE_INTL")"

echo "[bind] 推送完成: channel=$PUSH_CHANNEL target=$PUSH_TARGET"
