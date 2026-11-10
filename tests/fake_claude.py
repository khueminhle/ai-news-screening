"""A stand-in for `claude --print` used by the tests. Reads the prompt from stdin like the real CLI.

Behaviour depends on the item titles in the prompt:
  "SLEEP"     -> sleeps 60 s (to test the timeout kill)
  "BADQUOTE"  -> returns a figure whose quote is not in the text (to test quarantine)
  "GARBAGE"   -> replies with text that is not JSON
Otherwise it labels every item and quotes "$5.32" from the title.
"""
import json
import sys
import time

prompt = sys.stdin.buffer.read().decode("utf-8")

if prompt.startswith("# Write financial statement analysis"):
    out = [{"part": p, "text": f"The {p} view is consistent with the filing.", "cites": ["FS:IS"]}
           for p in ("overall", "IS", "BS", "CF")]
    print(json.dumps({"type": "result", "is_error": False, "result": json.dumps({"sentences": out})}))
    sys.exit(0)

if prompt.startswith("# Write dashboard narratives"):
    # One good sentence per part, one paragraph packed into one "sentence" (Python must split it),
    # and one sentence with an invented number (must be removed after the retry).
    body = prompt.split("## Fact sheet", 1)[1].split("## Fix these sentences", 1)
    sheet = json.loads(body[0].strip())
    ids = [f["id"] for f in sheet["facts"]]
    if len(body) > 1:                                   # retry: still wrong
        fixes = json.loads(body[1].split("\n", 2)[2])
        out = [{"part": f["part"], "text": f["text"], "cites": f["cites"], "fixes": f["index"]} for f in fixes]
    elif "macro" in prompt[:60]:
        out = [{"part": "big_picture", "text": "Rates and inflation point the same way.", "cites": [ids[0]]}]
        out += [{"part": i[2:], "text": "This series fits the picture.", "cites": [i]} for i in ids if i.startswith("U:")]
    else:
        out = [{"part": "stock", "text": "The stock moved with the market. News explained part of it.", "cites": [ids[0]]},
               {"part": "stock", "text": "It rose 99.99% in a day.", "cites": [ids[0]]}]
    print(json.dumps({"type": "result", "is_error": False, "result": json.dumps({"sentences": out})}))
    sys.exit(0)

if prompt.startswith("# Write digest bullets"):
    # Digest mode. Fact text keywords: INVENT (wrong number, fixed in the retry), ALWAYSBAD (wrong number twice),
    # ADVICE (advice in own voice twice), FAKECITE (cites an unknown ID). Otherwise a correct bullet.
    body = prompt.split("## Fact sheet", 1)[1].split("## Fix these bullets", 1)
    sheet = json.loads(body[0].strip())
    retry = json.loads(body[1].split("\n", 2)[2]) if len(body) > 1 else None
    bullets = []
    if retry is not None:
        for f in retry:
            if "INVENT" in f["text"]:
                bullets.append({"text": "Revenue was $1.026B.", "cites": f["cites"], "fixes": f["index"]})
            else:                                      # ALWAYSBAD / ADVICE stay bad
                bullets.append({"text": f["text"], "cites": f["cites"], "fixes": f["index"]})
    else:
        for fact in sheet["facts"]:
            if not fact["id"].startswith(("N", "S", "F")):
                continue
            t = fact["text"]
            if "INVENT" in t:
                bullets.append({"text": "INVENT revenue was $9.99B.", "cites": [fact["id"]]})
            elif "ALWAYSBAD" in t:
                bullets.append({"text": "ALWAYSBAD revenue was $7.77B.", "cites": [fact["id"]]})
            elif "ADVICE" in t:
                bullets.append({"text": "ADVICE: investors should buy the stock.", "cites": [fact["id"]]})
            elif "FAKECITE" in t:
                bullets.append({"text": "Something happened.", "cites": ["N999999"]})
            else:
                bullets.append({"text": "Revenue was $1.026B.", "cites": [fact["id"]]})
    print(json.dumps({"type": "result", "is_error": False, "result": json.dumps({"bullets": bullets})}))
    sys.exit(0)

payload =json.loads(prompt[prompt.rfind("## Input"):].split("\n", 2)[2]) if "## Input" in prompt else {"items": []}
titles = " ".join(i.get("title", "") for i in payload["items"])

if "SLEEP" in titles or "SLEEP" in prompt[:200]:
    time.sleep(60)
if "GARBAGE" in titles:
    print(json.dumps({"type": "result", "is_error": False, "result": "sorry, I cannot do that"}))
    sys.exit(0)

labels = []
for it in payload["items"]:
    quote = "made-up words $9.99" if "BADQUOTE" in it["title"] else "EPS $5.32"
    labels.append({"item_id": it["item_id"], "relevant": True, "tickers": it.get("tickers_tagged", []),
                   "event_type": "earnings", "importance": "high", "importance_reason": "results",
                   "sentiment": "positive", "fiscal_period": "fiscal Q1 2027",
                   "figures": [{"label": "EPS", "value": "$5.32", "quote": quote}],
                   "near_duplicate_of": None, "one_line": "Results were reported."})
reply = "```json\n" + json.dumps({"items": labels}) + "\n```"     # fenced on purpose, like real Claude sometimes is
print(json.dumps({"type": "result", "is_error": False, "result": reply, "usage": {"output_tokens": 10}}))
