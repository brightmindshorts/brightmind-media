import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from reel_renderer import render_reel


ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

STATE = Path("state.json")
BASE = "https://graph.instagram.com/v25.0"
TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
USER_ID = os.environ.get("INSTAGRAM_USER_ID", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"


def redact(message):
    for name in ("INSTAGRAM_ACCESS_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        secret = os.environ.get(name, "").strip()
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"https://[^/\s@]+@", "https://[REDACTED]@", message)
    return message


def git(*args):
    result = subprocess.run(
        ["git", *args], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise RuntimeError(
            "Git command failed: " + " ".join(args) + "\n"
            + redact(result.stdout)
        ) from None
    return result.stdout.strip()


def push_records():
    for attempt in range(3):
        try:
            git("push", "origin", "HEAD:main")
            return
        except RuntimeError as error:
            print(str(error), flush=True)
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
            git("fetch", "origin", "main")
            base = git("merge-base", "HEAD", "origin/main")
            remote_state = git(
                "diff", "--name-only", base, "origin/main", "--", "state.json"
            )
            if remote_state:
                raise RuntimeError(
                    "Remote publishing state changed concurrently. "
                    "Stopped to protect publishing records; inspect state.json."
                )
            git("rebase", "origin/main")


def save(state, assets=None):
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    paths = ["state.json"]
    if assets:
        paths.append(str(assets))
    git("add", "--", *paths)
    if git("diff", "--cached", "--name-only"):
        git("commit", "-m", "Update publishing state and media")
    push_records()


def repair_records():
    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists() else {"entries": {}}
    )
    repaired = []
    for key, entry in state["entries"].items():
        if entry.get("abandoned"):
            continue
        if entry.get("post") == {} and entry.get("story") == {}:
            entry["abandoned"] = True
            entry["abandon_reason"] = (
                "Both parts were empty before any Meta creation intent. "
                "Unused content released without publishing."
            )
            repaired.append(key)
    state["last_repository_check"] = datetime.now(
        ZoneInfo("Europe/Istanbul")
    ).isoformat()
    save(state)
    print("Repository write/push check passed.")
    print("Released empty records:", ", ".join(repaired) or "none")
    print("No Instagram API calls or publications were made.")
    unresolved = [
        key for key, entry in state["entries"].items()
        if not entry.get("abandoned")
        and any(entry.get(part, {}).get("status") != "done"
                for part in ("post", "story"))
    ]
    if unresolved:
        raise RuntimeError(
            "Records with possible Meta activity were preserved for review: "
            + ", ".join(unresolved)
        )


def api(path, data=None):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(
        BASE + "/" + path,
        data=body,
        headers={"Authorization": "Bearer " + TOKEN},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.load(error).get("error", {})
        except Exception:
            detail = {}
        message = str(detail.get("message", "Request failed"))
        raise RuntimeError(
            f"Meta HTTP {error.code}: "
            + message.replace(TOKEN, "[REDACTED]")
        ) from None
    except Exception:
        raise RuntimeError(
            "Network error. Publishing state was retained; inspect before retrying."
        ) from None


def render(text, folder):
    folder.mkdir(parents=True, exist_ok=True)

    for name, height in (("post", 1350), ("story", 1920)):
        image = Image.new("RGB", (1080, height), "black")
        draw = ImageDraw.Draw(image)
        lines = text.splitlines()

        for size in range(66, 29, -2):
            font = ImageFont.truetype(FONT, size)
            if max(draw.textlength(line, font=font) for line in lines) <= 900:
                break
        else:
            raise RuntimeError("Text is too wide for the template.")

        box = draw.multiline_textbbox(
            (0, 0), text, font=font, spacing=22
        )
        text_height = box[3] - box[1]
        if text_height > height - 500:
            raise RuntimeError("Text is too tall for the template.")

        y = (height - text_height) / 2 - box[1]
        draw.multiline_text(
            (90, y), text, font=font, fill="white", spacing=22
        )
        image.save(folder / f"{name}.jpg", quality=95)


def fingerprint(item):
    return hashlib.sha256(item["text"].encode()).hexdigest()


def publish_part(state, entry, kind, prepare_only=False):
    part = entry.setdefault(kind, {})

    if part.get("status") == "done":
        print(kind, "already published")
        return

    if part.get("status") == "creating":
        raise RuntimeError(
            f"{kind}: previous container creation was interrupted. "
            "Inspect the previous run before changing state."
        )

    if not part.get("container"):
        part["status"] = "creating"
        save(state)

        video = kind == "post" and entry.get("format") == "reel"
        data = {"video_url" if video else "image_url": entry["urls"][kind]}
        if video:
            data.update(media_type="REELS", share_to_feed="true", cover_url=entry["urls"]["cover"])
        if kind == "story":
            data["media_type"] = "STORIES"
        else:
            data["caption"] = entry["caption"]

        result = api(USER_ID + "/media", data)
        container = result.get("id")
        if not container:
            raise RuntimeError("Meta did not return a container ID.")

        part.update(container=container, status="processing")
        save(state)

    container = part["container"]
    previous_publish_uncertain = part.get("status") == "publishing"

    for attempt in range(12):
        result = api(container + "?fields=status_code")
        status = result.get("status_code")
        print(kind, "container", container, "status", status, flush=True)

        if status == "PUBLISHED":
            part["status"] = "done"
            save(state)
            return

        if previous_publish_uncertain:
            raise RuntimeError(
                f"{kind}: previous publish result is uncertain. "
                "Check Instagram and the container before retrying."
            )

        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"{kind}: container is {status}")
        time.sleep(30)
    else:
        raise RuntimeError("Media is still processing; its ID was saved.")

    if prepare_only:
        part["status"] = "ready"
        save(state)
        print(kind, "READY; not published", flush=True)
        return

    part["status"] = "publishing"
    save(state)

    result = api(USER_ID + "/media_publish", {"creation_id": container})
    media_id = result.get("id")
    if not media_id:
        raise RuntimeError("No media ID returned; check Instagram.")

    part.update(status="done", media_id=media_id)
    save(state)
    print(kind, "published:", media_id, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["preview", "publish", "repair", "stage"], required=True)
    parser.add_argument("--slot", choices=["12", "16", "20"], default="12")
    args = parser.parse_args()

    if args.mode == "repair":
        repair_records()
        return

    is_reel = args.slot == "16"
    bank_file = "reels.json" if is_reel else "content.json"
    items = json.loads(Path(bank_file).read_text(encoding="utf-8"))
    if not items:
        raise RuntimeError("The content bank is empty.")

    if args.mode == "preview":
        if is_reel:
            render_reel(items[0], Path("preview"))
        else:
            render(items[0]["text"], Path("preview"))
        Path("preview/caption.txt").write_text(
            items[0]["caption"], encoding="utf-8"
        )
        print("Preview created. Nothing was published.")
        return

    if not TOKEN or not USER_ID.isdigit():
        raise RuntimeError("Instagram credentials are missing.")
    if REPO != "brightmindshorts/brightmind-media":
        raise RuntimeError("Unexpected repository.")

    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    hour = int(args.slot)
    if args.mode == "publish" and now.hour != hour:
        raise RuntimeError(
            "Outside the selected one-hour publishing window. Slot skipped."
        )

    account = api("me?fields=user_id,username")
    if (
        account.get("username", "").lower() != "brightmindshorts"
        or str(account.get("user_id", "")) != USER_ID
    ):
        raise RuntimeError("Unexpected Instagram account.")

    state = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists() else {"entries": {}}
    )
    entries = state["entries"]
    key = f"{now:%Y-%m-%d}-{args.slot}"

    if args.mode == "stage" and hour < now.hour:
        raise RuntimeError("Cannot stage a past slot")

    # Resolve incomplete work before creating a new pair of publications.
    pending = [
        name for name, value in entries.items()
        if name != key and not value.get("abandoned")
        and (
            value.get("post", {}).get("status") != "done"
            or value.get("story", {}).get("status") != "done"
        )
    ]
    if pending:
        raise RuntimeError("An earlier item needs attention: " + pending[0])

    if key not in entries:
        used = {
            value["fingerprint"] for value in entries.values()
            if not value.get("abandoned")
        }
        item = next(
            (item for item in items if fingerprint(item) not in used), None
        )
        if item is None:
            raise RuntimeError(
                "Content bank exhausted. Add new original items to content.json."
            )

        folder = Path("generated") / key
        if is_reel:
            render_reel(item, folder)
        else:
            render(item["text"], folder)
        entry = {
            "format": "reel" if is_reel else "image",
            "fingerprint": fingerprint(item),
            "text": item["text"],
            "caption": item["caption"],
            "post": {},
            "story": {},
        }
        entries[key] = entry
        save(state, folder)

        # Immutable URLs prevent a later update from changing this publication.
        revision = git("rev-parse", "HEAD")
        entry["urls"] = {
            kind: (
                f"https://raw.githubusercontent.com/{REPO}/{revision}/"
                f"{folder.as_posix()}/{'reel.mp4' if kind == 'post' and is_reel else kind + '.jpg'}"
            )
            for kind in ("post", "story")
        }
        if is_reel:
            entry["urls"]["cover"] = (
                f"https://raw.githubusercontent.com/{REPO}/{revision}/"
                f"{folder.as_posix()}/cover.jpg"
            )
        save(state)
    else:
        entry = entries[key]
        if "urls" not in entry:
            raise RuntimeError("Asset setup was interrupted; inspect state.json.")

    prepare_only = args.mode == "stage"
    publish_part(state, entry, "post", prepare_only)
    publish_part(state, entry, "story", prepare_only)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write(f"{'Prepared (not published)' if prepare_only else 'Published'} post and story for `{key}`.\n")


if __name__ == "__main__":
    main()
