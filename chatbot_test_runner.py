#!/usr/bin/env python3
"""ADB chatbot test runner.

This script automates a chat-style Android app using ADB and extracts bot replies
from UIAutomator XML (UI dump only).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
from PIL import Image
import pytesseract
from pytesseract import TesseractNotFoundError


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
INTERNAL_NODE_TEXT_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
IGNORED_TEXT_SUBSTRINGS = (
    "本服務由 AI 提供",
    "AI 告知聲明",
    "注意事項",
)
FOOTER_DISCLAIMER_RE = re.compile(
    r"本服務由\s*AI\s*提供|告知聲明|注意事項|內容真實度|使用前詳閱"
)


@dataclass
class Point:
    x: int
    y: int


@dataclass
class Region:
    left: int
    top: int
    right: int
    bottom: int

    def contains_box(self, box: Tuple[int, int, int, int]) -> bool:
        x1, y1, x2, y2 = box
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2
        return self.left <= cx <= self.right and self.top <= cy <= self.bottom


@dataclass
class CubeTaskLookupConfig:
    enabled: bool
    base_url: str
    middle_id: str
    token: Optional[str]
    session_id: Optional[str]
    did: Optional[str]
    chat_session_id: Optional[str]
    api_key: str
    model: str
    app_version: str
    version: str
    user_agent: str

    def is_ready(self) -> bool:
        return bool(
            self.enabled
            and self.token
            and self.session_id
            and self.did
            and self.chat_session_id
        )


def _cube_api_headers(cfg: CubeTaskLookupConfig) -> dict:
    return {
        "Accept": "application/json",
        "Accept-Charset": "UTF-8",
        "Apikey": cfg.api_key,
        "AppVersion": cfg.app_version,
        "ClientTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "DID": cfg.did or "",
        "HasSec": "N",
        "Locale": "zh_TW",
        "MiddleID": cfg.middle_id,
        "Model": cfg.model,
        "SessionID": cfg.session_id or "",
        "SystemName": "Android",
        "Token": cfg.token or "",
        "TxnUUID": str(uuid.uuid4()),
        "User-Agent": cfg.user_agent,
        "Version": cfg.version,
        "Content-Type": "application/json",
    }


def try_lookup_task_id_via_caichat04(
    cfg: CubeTaskLookupConfig,
    prompt: str,
    *,
    max_page: int = 3,
    page_size: int = 20,
    timeout_sec: float = 20.0,
) -> Optional[str]:
    if not cfg.is_ready():
        return None

    with httpx.Client(timeout=timeout_sec) as client:
        for page in range(1, max_page + 1):
            payload = {
                "RqData": {
                    "Pagination": {"Page": str(page), "Size": str(page_size)},
                    "ChatSessionID": cfg.chat_session_id,
                }
            }
            resp = client.post(cfg.base_url, headers=_cube_api_headers(cfg), json=payload)
            resp.raise_for_status()
            body = resp.json()
            if body.get("StatusCode") != "0000":
                continue

            records = body.get("RsData", {}).get("ChatRecords", [])
            if not records:
                continue

            by_task: dict = {}
            for r in records:
                task_id = (r.get("TaskID") or "").strip()
                if not task_id:
                    continue
                entry = by_task.setdefault(task_id, {"user": None, "assistant": None})
                role = (r.get("Role") or "").strip().lower()
                if role == "user":
                    entry["user"] = r
                elif role == "assistant":
                    entry["assistant"] = r

            for task_id, pair in by_task.items():
                user_rec = pair.get("user")
                assistant_rec = pair.get("assistant")
                if not user_rec or not assistant_rec:
                    continue
                if (user_rec.get("Content") or "").strip() != prompt.strip():
                    continue
                if (assistant_rec.get("TaskStatus") or "").strip().lower() != "completed":
                    continue
                if not (assistant_rec.get("Content") or "").strip():
                    continue
                return task_id
    return None


DEFAULT_CUBE_BETA_PACKAGE = "com.cathaybk.pokemon.mew"
DEFAULT_CUBE_BETA_ACTIVITY = "com.cathaybk.pokemon.ui.MainActivity"
DEFAULT_CUBE_BETA_PROMPT = "請問最近一筆交易紀錄是什麼？"
DEFAULT_CUBE_BETA_WAIT_SEC = 130.0
DEFAULT_CHATGPT_PACKAGE = "com.openai.chatgpt"
DEFAULT_CHATGPT_PROMPT = "你好"
DEFAULT_CHATGPT_INPUT_POINT = Point(714, 2685)
DEFAULT_CHATGPT_SEND_POINT = Point(1285, 1589)
DEFAULT_CHATGPT_RESPONSE_REGION = Region(0, 370, 1440, 2505)
DEFAULT_CHATGPT_WAIT_SEC = 8.0
DEFAULT_CUBE_BETA_API_URL = "https://cubebetaut.cathaybkdev.com.tw/cubebeta/rest"
DEFAULT_CUBE_BETA_CAICHAT04_MIDDLE_ID = "CAICHAT04"
DEFAULT_CUBE_BETA_API_KEY = "a3d089098460a07d"
DEFAULT_CUBE_BETA_MODEL = "SM-N9750"
DEFAULT_CUBE_BETA_VERSION = "7.8.1001"
DEFAULT_CUBE_BETA_APP_VERSION = "7.8.1001"
DEFAULT_CUBE_BETA_USER_AGENT = "CubeBeta-Android/1.0.2"
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"


class ADBChatbotTester:
    def __init__(
        self,
        device_serial: Optional[str],
        package_name: str,
        activity_name: Optional[str],
        input_point: Optional[Point],
        send_point: Optional[Point],
        response_region: Optional[Region],
        output_dir: Path,
        ocr_lang: str,
        adb_keyboard_ime: Optional[str],
        send_by_enter: bool,
    ) -> None:
        self.device_serial = device_serial
        self.package_name = package_name
        self.activity_name = activity_name
        self.input_point = input_point
        self.send_point = send_point
        self.response_region = response_region
        self.output_dir = output_dir
        self.ocr_lang = ocr_lang
        self.adb_keyboard_ime = adb_keyboard_ime
        self.send_by_enter = send_by_enter

    def _adb_prefix(self) -> List[str]:
        cmd = ["adb"]
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])
        return cmd

    def _run_adb(self, args: List[str], check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        cmd = self._adb_prefix() + args
        return subprocess.run(cmd, check=check, text=text, capture_output=True)

    def ensure_device_ready(self) -> None:
        devices = subprocess.run(["adb", "devices"], check=True, text=True, capture_output=True)
        lines = [line.strip() for line in devices.stdout.splitlines()[1:] if line.strip()]
        ready = [line for line in lines if line.endswith("\tdevice")]
        if not ready:
            raise RuntimeError("No ready Android device found. Please run: adb devices")
        if self.device_serial and not any(line.startswith(self.device_serial + "\t") for line in ready):
            raise RuntimeError(f"Device {self.device_serial} is not in ready state.")

    def launch_app(self) -> None:
        if self.activity_name:
            component = f"{self.package_name}/{self.activity_name}"
            self._run_adb(["shell", "am", "start", "-n", component])
        else:
            self._run_adb(["shell", "monkey", "-p", self.package_name, "-c", "android.intent.category.LAUNCHER", "1"])

    def tap(self, point: Point) -> None:
        self._run_adb(["shell", "input", "tap", str(point.x), str(point.y)])

    def press_keyevent(self, keycode: int) -> None:
        self._run_adb(["shell", "input", "keyevent", str(keycode)])

    def _get_default_input_method(self) -> str:
        result = self._run_adb(["shell", "settings", "get", "secure", "default_input_method"])
        return result.stdout.strip()

    def _list_input_methods(self) -> List[str]:
        result = self._run_adb(["shell", "ime", "list", "-s"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _resolve_adb_keyboard_ime(self) -> Optional[str]:
        installed_imes = self._list_input_methods()
        if self.adb_keyboard_ime:
            return self.adb_keyboard_ime if self.adb_keyboard_ime in installed_imes else None

        if ADB_KEYBOARD_IME in installed_imes:
            return ADB_KEYBOARD_IME

        for ime in installed_imes:
            lowered = ime.lower()
            if "adbkeyboard" in lowered or lowered.endswith("/.adbime"):
                return ime
        return None

    def _use_adb_keyboard_for_unicode(self, text: str) -> bool:
        target_ime = self._resolve_adb_keyboard_ime()
        if not target_ime:
            return False

        previous_ime = self._get_default_input_method()
        try:
            self._run_adb(["shell", "ime", "enable", target_ime], check=False)
            self._run_adb(["shell", "ime", "set", target_ime])
            # Give IME framework a short moment to finish switching before broadcast.
            time.sleep(0.3)
            sent = self._run_adb(
                ["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text],
                check=False,
            )
            # Let the IME consume the broadcast before switching back.
            time.sleep(0.25)
            return sent.returncode == 0
        finally:
            if previous_ime:
                self._run_adb(["shell", "ime", "set", previous_ime], check=False)
                time.sleep(0.2)

    def input_text(self, text: str) -> None:
        # Prefer ADB Keyboard broadcast for all text because it behaves closest
        # to paste and preserves spaces/Unicode reliably.
        if self._use_adb_keyboard_for_unicode(text):
            return

        is_ascii_only = all(ord(ch) < 128 for ch in text)
        if is_ascii_only:
            sanitized = text.replace(" ", "%s")
            self._run_adb(["shell", "input", "text", sanitized])
            return

        raise RuntimeError(
            "This device cannot input non-ASCII text with the stock adb text command, "
            "and no ADB Keyboard IME was found. Install ADB Keyboard on the device to "
            "send Chinese or other Unicode text automatically."
        )

    def scroll_to_bottom(self) -> None:
        """Fling the chat to the bottom so the latest reply is visible."""
        self.prepare_ui_targets()
        cx = (self.response_region.left + self.response_region.right) // 2
        cy_top = self.response_region.bottom - 100
        cy_bot = self.response_region.top + 100
        self._run_adb(
            ["shell", "input", "swipe",
             str(cx), str(cy_top), str(cx), str(cy_bot), "300"]
        )
        time.sleep(0.6)

    def dismiss_keyboard(self) -> None:
        self.prepare_ui_targets()
        dismiss_point = Point(
            (self.response_region.left + self.response_region.right) // 2,
            self.response_region.top + max(80, (self.response_region.bottom - self.response_region.top) // 3),
        )
        self.tap(dismiss_point)
        time.sleep(0.4)

    def send_message(self, message: str, post_send_wait_sec: float) -> None:
        self.prepare_ui_targets()
        self.tap(self.input_point)
        time.sleep(0.3)
        self.input_text(message)
        time.sleep(0.2)
        send_point = self.send_point or self.detect_send_point_from_ui()
        if send_point:
            self.tap(send_point)
        elif self.send_by_enter:
            self.press_keyevent(66)
        else:
            self.tap(self.send_point)
        time.sleep(post_send_wait_sec)
        self.dismiss_keyboard()
        self.scroll_to_bottom()

    def take_screenshot(self, output_file: Path) -> None:
        cmd = self._adb_prefix() + ["exec-out", "screencap", "-p"]
        with output_file.open("wb") as f:
            subprocess.run(cmd, check=True, stdout=f)

    def get_uiautomator_xml(self) -> str:
        self._run_adb(["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"])
        dump = self._adb_prefix() + ["exec-out", "cat", "/sdcard/window_dump.xml"]
        proc = subprocess.run(dump, check=True, capture_output=True)
        content = proc.stdout.decode("utf-8", errors="ignore")
        if "<?xml" not in content:
            raise RuntimeError("Failed to get UI XML dump.")
        return content

    @staticmethod
    def _parse_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
        m = BOUNDS_RE.match(bounds_str)
        if not m:
            return None
        return tuple(int(g) for g in m.groups())

    @staticmethod
    def _bounds_center(bounds: Tuple[int, int, int, int]) -> Point:
        left, top, right, bottom = bounds
        return Point((left + right) // 2, (top + bottom) // 2)

    @staticmethod
    def _parse_xml_root(xml_content: str) -> ET.Element:
        return ET.fromstring(xml_content)

    def _find_first_node(
        self,
        root: ET.Element,
        *,
        class_name: Optional[str] = None,
        text_equals: Optional[str] = None,
        content_desc_equals: Optional[str] = None,
        clickable: Optional[str] = None,
        enabled: Optional[str] = None,
    ) -> Optional[ET.Element]:
        for node in root.iter("node"):
            attrib = node.attrib
            if class_name and attrib.get("class") != class_name:
                continue
            if text_equals is not None and (attrib.get("text") or "").strip() != text_equals:
                continue
            if content_desc_equals is not None and (attrib.get("content-desc") or "").strip() != content_desc_equals:
                continue
            if clickable is not None and attrib.get("clickable") != clickable:
                continue
            if enabled is not None and attrib.get("enabled") != enabled:
                continue
            return node
        return None

    def _find_node_bounds(
        self,
        xml_content: str,
        *,
        class_name: Optional[str] = None,
        text_equals: Optional[str] = None,
        content_desc_equals: Optional[str] = None,
        clickable: Optional[str] = None,
        enabled: Optional[str] = None,
    ) -> Optional[Tuple[int, int, int, int]]:
        root = self._parse_xml_root(xml_content)
        node = self._find_first_node(
            root,
            class_name=class_name,
            text_equals=text_equals,
            content_desc_equals=content_desc_equals,
            clickable=clickable,
            enabled=enabled,
        )
        if node is None:
            return None
        return self._parse_bounds(node.attrib.get("bounds", ""))

    def detect_input_point_from_ui(self) -> Optional[Point]:
        xml_content = self.get_uiautomator_xml()
        bounds = self._find_node_bounds(xml_content, class_name="android.widget.EditText", clickable="true")
        if not bounds:
            return None
        return self._bounds_center(bounds)

    def detect_send_point_from_ui(self) -> Optional[Point]:
        xml_content = self.get_uiautomator_xml()
        for content_desc in ("傳送訊息", "送出", "發送", "Send"):
            bounds = self._find_node_bounds(xml_content, content_desc_equals=content_desc)
            if bounds:
                return self._bounds_center(bounds)
        return None

    def detect_response_region_from_ui(self) -> Optional[Region]:
        xml_content = self.get_uiautomator_xml()
        root = self._parse_xml_root(xml_content)
        first_node = root.find("node")
        root_bounds = self._parse_bounds(first_node.attrib.get("bounds", "")) if first_node is not None else None
        if not root_bounds:
            return None

        edit_bounds = self._find_node_bounds(xml_content, class_name="android.widget.EditText", clickable="true")
        if not edit_bounds:
            return None

        header_bottom = 0
        for content_desc in ("Back", "功能表", "登入", "record", "Save"):
            bounds = self._find_node_bounds(xml_content, content_desc_equals=content_desc)
            if bounds:
                header_bottom = max(header_bottom, bounds[3])

        left, top, right, bottom = root_bounds
        return Region(left, header_bottom + 20, right, edit_bounds[1] - 20)

    def capture_visible_ui_snapshot(self) -> dict:
        xml_content = self.get_uiautomator_xml()
        return {
            "package": self.package_name,
            "activity": self.activity_name,
            "extract_method": "ui_dump",
            "ui_texts": self.extract_text_from_ui_dump(xml_content),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    def prepare_ui_targets(self) -> None:
        if self.input_point and self.response_region and (self.send_point or self.send_by_enter):
            return

        if not self.input_point:
            self.input_point = self.detect_input_point_from_ui()

        if not self.send_point and not self.send_by_enter:
            self.send_point = self.detect_send_point_from_ui()

        if not self.response_region:
            self.response_region = self.detect_response_region_from_ui()

        missing = []
        if not self.input_point:
            missing.append("input point")
        if not self.send_by_enter and not self.send_point:
            missing.append("send point")
        if not self.response_region:
            missing.append("response region")
        if missing:
            raise RuntimeError("Could not auto-detect required UI targets: " + ", ".join(missing))

    def extract_text_from_ui_dump(self, xml_content: str, sent_prompt: str = "") -> List[str]:
        root = ET.fromstring(xml_content)
        collected: List[str] = []
        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            if not text:
                continue
            if INTERNAL_NODE_TEXT_RE.match(text):
                continue
            normalized = " ".join(text.split())
            if any(token in normalized for token in IGNORED_TEXT_SUBSTRINGS):
                continue
            bounds = self._parse_bounds(node.attrib.get("bounds", ""))
            if not bounds:
                continue
            if self.response_region.contains_box(bounds):
                collected.append((bounds, text))
        # Keep order while removing exact duplicates.
        seen: set = set()
        unique = []
        for bounds, t in collected:
            if t in seen:
                continue
            seen.add(t)
            unique.append((bounds, t))
        # Drop nodes that exactly match what the user sent.
        if sent_prompt:
            unique = [(b, t) for b, t in unique if t != sent_prompt]
        # Sort top-to-bottom so the last item is the most recent message.
        unique.sort(key=lambda x: x[0][1])
        texts = [t for _, t in unique]

        # If footer disclaimer appears, drop it and everything after it.
        for idx, t in enumerate(texts):
            if FOOTER_DISCLAIMER_RE.search(" ".join(t.split())):
                texts = texts[:idx]
                break

        return texts

    @staticmethod
    def _subtract_existing_texts(before_texts: List[str], after_texts: List[str]) -> List[str]:
        remaining = list(before_texts)
        new_texts: List[str] = []

        for text in after_texts:
            if text in remaining:
                remaining.remove(text)
                continue
            new_texts.append(text)

        return new_texts

    def capture_visible_ui_texts(self, sent_prompt: str = "") -> List[str]:
        xml_content = self.get_uiautomator_xml()
        return self.extract_text_from_ui_dump(xml_content, sent_prompt=sent_prompt)

    def wait_until_response_stable(
        self,
        baseline_ui_texts: List[str],
        sent_prompt: str,
        max_wait_sec: float,
        stable_window_sec: float = 8.0,
        poll_interval_sec: float = 2.0,
    ) -> List[str]:
        deadline = time.monotonic() + max_wait_sec
        last_change_at = time.monotonic()
        latest_new_texts: List[str] = []

        while True:
            try:
                ui_texts = self.capture_visible_ui_texts(sent_prompt=sent_prompt)
                new_texts = self._subtract_existing_texts(baseline_ui_texts, ui_texts)
            except Exception:
                new_texts = latest_new_texts

            if new_texts != latest_new_texts:
                latest_new_texts = new_texts
                last_change_at = time.monotonic()

            now = time.monotonic()
            has_answer = bool(latest_new_texts)
            is_stable = (now - last_change_at) >= stable_window_sec
            timed_out = now >= deadline
            if (has_answer and is_stable) or timed_out:
                return latest_new_texts

            time.sleep(poll_interval_sec)

    def extract_text_by_ocr(self, screenshot_file: Path) -> str:
        image = Image.open(screenshot_file)
        crop = image.crop(
            (
                self.response_region.left,
                self.response_region.top,
                self.response_region.right,
                self.response_region.bottom,
            )
        )
        # psm 6 is usually stable for chat-bubble paragraph extraction.
        config = "--oem 3 --psm 6"
        try:
            return pytesseract.image_to_string(crop, lang=self.ocr_lang, config=config).strip()
        except TesseractNotFoundError as exc:
            raise RuntimeError(
                "OCR fallback requires tesseract to be installed and available in PATH. "
                "Either install tesseract or rely on direct UI extraction only."
            ) from exc

    def run_test_case(
        self,
        case_id: int,
        prompt: str,
        wait_sec: float,
        task_lookup_cfg: Optional[CubeTaskLookupConfig] = None,
    ) -> dict:
        case_dir = self.output_dir / f"case_{case_id:03d}"
        case_dir.mkdir(parents=True, exist_ok=True)

        self.prepare_ui_targets()

        baseline_ui_texts: List[str] = []
        try:
            baseline_ui_texts = self.capture_visible_ui_texts()
        except Exception:
            baseline_ui_texts = []

        started_at = datetime.now().isoformat(timespec="seconds")
        # Keep post-send delay short; response completion is handled by stability wait.
        self.send_message(prompt, post_send_wait_sec=min(1.2, max(0.6, wait_sec * 0.05)))

        screenshot_file = case_dir / "screen.png"
        self.take_screenshot(screenshot_file)

        method = "ui_dump"
        response_text = ""

        try:
            ui_texts = self.wait_until_response_stable(
                baseline_ui_texts,
                prompt,
                max_wait_sec=wait_sec,
            )
            response_text = "\n".join(ui_texts).strip()
        except Exception:
            response_text = ""

        if not response_text:
            method = "ui_dump_empty"

        result = {
            "case_id": case_id,
            "prompt": prompt,
            "response": response_text,
            "extract_method": method,
            "screenshot": str(screenshot_file),
            "started_at": started_at,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        if method == "ui_dump_empty":
            result["warning"] = "No new UI text extracted for this case. OCR fallback is disabled."

        if task_lookup_cfg and task_lookup_cfg.is_ready():
            try:
                result["task_id"] = try_lookup_task_id_via_caichat04(task_lookup_cfg, prompt)
                result["task_id_lookup"] = "caichat04"
            except Exception as exc:
                result["task_id"] = None
                result["task_id_lookup"] = f"error: {exc}"

        with (case_dir / "result.json").open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        return result


def append_transcript(transcript_file: Path, result: dict) -> None:
    lines = [
        f"[{result['timestamp']}] CASE {result['case_id']}",
        f"TASK_ID: {result.get('task_id', '')}",
        f"USER: {result['prompt']}",
        f"BOT ({result['extract_method']}): {result['response']}",
        "-" * 60,
    ]
    with transcript_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def parse_point(raw: str) -> Point:
    x, y = raw.split(",")
    return Point(int(x), int(y))


def parse_region(raw: str) -> Region:
    left, top, right, bottom = [int(n) for n in raw.split(",")]
    return Region(left, top, right, bottom)


def optional_point(raw: Optional[str]) -> Optional[Point]:
    if not raw:
        return None
    return parse_point(raw)


def optional_region(raw: Optional[str]) -> Optional[Region]:
    if not raw:
        return None
    return parse_region(raw)


def load_prompts(prompt: Optional[str], prompt_file: Optional[Path]) -> List[str]:
    if prompt:
        return [prompt]
    if prompt_file:
        lines = [line.strip() for line in prompt_file.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line]
    raise ValueError("Provide --prompt or --prompt-file")


def interactive_prompts() -> List[str]:
    print("Interactive mode: type your question and press Enter.")
    print("Press Enter on an empty line to finish.")
    prompts: List[str] = []
    while True:
        raw = input("Q> ").strip()
        if not raw:
            break
        prompts.append(raw)
    return prompts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automate chatbot app via ADB and extract replies")
    p.add_argument("--device-serial", default=None, help="ADB device serial")
    p.add_argument("--package", default=None, help="App package name, e.g. com.example.chat")
    p.add_argument("--activity", default=None, help="Activity name, e.g. .MainActivity")
    p.add_argument("--prompt", default=None, help="Single prompt to send")
    p.add_argument("--prompt-file", type=Path, default=None, help="Text file of prompts (one line per prompt)")
    p.add_argument(
        "--interactive",
        action="store_true",
        help="Read prompts interactively from terminal input",
    )
    p.add_argument("--input-point", default=None, help="Input box tap coordinate: x,y")
    p.add_argument("--send-point", default=None, help="Send button tap coordinate: x,y")
    p.add_argument(
        "--response-region",
        default=None,
        help="Bot response region for extraction: left,top,right,bottom",
    )
    p.add_argument("--wait-sec", type=float, default=4.0, help="Wait time after send before capture")
    p.add_argument("--output-dir", type=Path, default=Path("outputs"), help="Output directory")
    p.add_argument(
        "--ocr-lang",
        default="chi_tra+eng",
        help="Tesseract language pack, e.g. chi_tra+eng or eng",
    )
    p.add_argument(
        "--session-name",
        default="chat_session",
        help="Output transcript base name (without extension)",
    )
    p.add_argument(
        "--chatgpt-auto-once",
        action="store_true",
        help="Run one ChatGPT round automatically with built-in defaults",
    )
    p.add_argument(
        "--cube-beta-auto-once",
        action="store_true",
        help="Run one Cube beta round automatically with built-in defaults and UI auto-detection",
    )
    p.add_argument(
        "--adb-keyboard-ime",
        default=None,
        help="ADB Keyboard IME id, e.g. com.android.adbkeyboard/.AdbIME",
    )
    p.add_argument(
        "--send-by-enter",
        action="store_true",
        help="Submit the message with Enter keyevent instead of tapping a send button",
    )
    p.add_argument(
        "--capture-current-ui",
        action="store_true",
        help="Capture currently visible UI texts via UIAutomator without sending a prompt",
    )
    p.add_argument(
        "--fetch-task-id",
        action="store_true",
        help="Lookup TaskID from Cube beta CAICHAT04 chat records after each response",
    )
    p.add_argument("--chat-session-id", default=None, help="Cube beta ChatSessionID for CAICHAT04 lookup")
    p.add_argument("--api-token", default=None, help="Cube beta API Token for CAICHAT04 lookup")
    p.add_argument("--api-session-id", default=None, help="Cube beta SessionID for CAICHAT04 lookup")
    p.add_argument("--api-did", default=None, help="Cube beta DID for CAICHAT04 lookup")
    p.add_argument("--api-base-url", default=DEFAULT_CUBE_BETA_API_URL, help="Cube beta API URL")
    p.add_argument("--api-middle-id", default=DEFAULT_CUBE_BETA_CAICHAT04_MIDDLE_ID, help="Task lookup MiddleID")
    p.add_argument("--api-key", default=DEFAULT_CUBE_BETA_API_KEY, help="Cube beta API key")
    p.add_argument("--api-model", default=DEFAULT_CUBE_BETA_MODEL, help="Device model header")
    p.add_argument("--api-app-version", default=DEFAULT_CUBE_BETA_APP_VERSION, help="AppVersion header")
    p.add_argument("--api-version", default=DEFAULT_CUBE_BETA_VERSION, help="Version header")
    p.add_argument("--api-user-agent", default=DEFAULT_CUBE_BETA_USER_AGENT, help="User-Agent header")
    p.epilog = (
        "Examples:\n"
        "  python chatbot_test_runner.py --chatgpt-auto-once\n"
        "  python chatbot_test_runner.py --cube-beta-auto-once --capture-current-ui\n"
        "  python chatbot_test_runner.py --package com.openai.chatgpt --prompt \"Hello\" "
        "--input-point 714,2685 --send-point 1285,1589 --response-region 0,370,1440,2505"
    )
    return p


def validate_required_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.capture_current_ui:
        return

    missing = []
    if not args.package:
        missing.append("--package")
    if not args.input_point and not args.cube_beta_auto_once:
        missing.append("--input-point")
    if not args.send_point and not args.send_by_enter and not args.cube_beta_auto_once:
        missing.append("--send-point")
    if not args.response_region and not args.cube_beta_auto_once:
        missing.append("--response-region")
    if missing:
        parser.error(
            "Missing required arguments: "
            + ", ".join(missing)
            + ". Use --chatgpt-auto-once or --cube-beta-auto-once for built-in presets."
        )


def apply_chatgpt_auto_defaults(args: argparse.Namespace) -> argparse.Namespace:
    args.package = args.package or DEFAULT_CHATGPT_PACKAGE
    args.prompt = args.prompt or DEFAULT_CHATGPT_PROMPT
    args.input_point = args.input_point or f"{DEFAULT_CHATGPT_INPUT_POINT.x},{DEFAULT_CHATGPT_INPUT_POINT.y}"
    args.send_point = args.send_point or f"{DEFAULT_CHATGPT_SEND_POINT.x},{DEFAULT_CHATGPT_SEND_POINT.y}"
    args.response_region = args.response_region or (
        f"{DEFAULT_CHATGPT_RESPONSE_REGION.left},{DEFAULT_CHATGPT_RESPONSE_REGION.top},"
        f"{DEFAULT_CHATGPT_RESPONSE_REGION.right},{DEFAULT_CHATGPT_RESPONSE_REGION.bottom}"
    )
    if args.wait_sec == 4.0:
        args.wait_sec = DEFAULT_CHATGPT_WAIT_SEC
    if args.session_name == "chat_session":
        args.session_name = "chatgpt_auto"
    args.activity = None
    args.prompt_file = None
    return args


def apply_cube_beta_auto_defaults(args: argparse.Namespace) -> argparse.Namespace:
    args.package = args.package or DEFAULT_CUBE_BETA_PACKAGE
    args.activity = args.activity or DEFAULT_CUBE_BETA_ACTIVITY
    if not args.prompt_file:
        args.prompt = args.prompt or DEFAULT_CUBE_BETA_PROMPT
    if args.wait_sec == 4.0:
        args.wait_sec = DEFAULT_CUBE_BETA_WAIT_SEC
    if args.session_name == "chat_session":
        args.session_name = "cube_beta_auto"
    args.send_by_enter = True if not args.send_point else args.send_by_enter
    return args


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.chatgpt_auto_once:
        args = apply_chatgpt_auto_defaults(args)
        if args.interactive:
            prompts = interactive_prompts()
        elif args.prompt_file:
            prompts = load_prompts(None, args.prompt_file)
        else:
            prompts = [args.prompt]
    elif (
        args.cube_beta_auto_once
        or len(sys.argv) == 1
        or (args.interactive and not args.package and not args.activity)
        or (args.prompt_file and not args.package and not args.activity)
    ):
        args = apply_cube_beta_auto_defaults(args)
        if args.interactive:
            prompts = interactive_prompts()
        elif args.prompt_file:
            prompts = load_prompts(None, args.prompt_file)
        else:
            prompts = [args.prompt]
    elif args.capture_current_ui:
        if not args.package:
            parser.error("--capture-current-ui requires --package or a built-in preset.")
        prompts = []
    else:
        validate_required_args(args, parser)
        prompts = interactive_prompts() if args.interactive else load_prompts(args.prompt, args.prompt_file)

    if args.interactive and not prompts and not args.capture_current_ui:
        parser.error("No prompt entered in interactive mode.")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    tester = ADBChatbotTester(
        device_serial=args.device_serial,
        package_name=args.package,
        activity_name=args.activity,
        input_point=optional_point(args.input_point),
        send_point=optional_point(args.send_point),
        response_region=optional_region(args.response_region),
        output_dir=args.output_dir,
        ocr_lang=args.ocr_lang,
        adb_keyboard_ime=args.adb_keyboard_ime,
        send_by_enter=args.send_by_enter,
    )

    tester.ensure_device_ready()
    tester.launch_app()
    time.sleep(1.5)

    task_lookup_cfg = CubeTaskLookupConfig(
        enabled=args.fetch_task_id,
        base_url=args.api_base_url,
        middle_id=args.api_middle_id,
        token=args.api_token,
        session_id=args.api_session_id,
        did=args.api_did,
        chat_session_id=args.chat_session_id,
        api_key=args.api_key,
        model=args.api_model,
        app_version=args.api_app_version,
        version=args.api_version,
        user_agent=args.api_user_agent,
    )

    if args.capture_current_ui:
        tester.prepare_ui_targets()
        snapshot = tester.capture_visible_ui_snapshot()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        if tester.input_point:
            print(f"Detected input point: {tester.input_point.x},{tester.input_point.y}")
        if tester.send_point:
            print(f"Detected send point: {tester.send_point.x},{tester.send_point.y}")
        if tester.response_region:
            print(
                "Detected response region: "
                f"{tester.response_region.left},{tester.response_region.top},"
                f"{tester.response_region.right},{tester.response_region.bottom}"
            )
        return 0

    result_file = args.output_dir / "summary.jsonl"
    transcript_file = args.output_dir / f"{args.session_name}_transcript.txt"
    with result_file.open("a", encoding="utf-8") as summary:
        for i, prompt in enumerate(prompts, start=1):
            result = tester.run_test_case(i, prompt, args.wait_sec, task_lookup_cfg=task_lookup_cfg)
            summary.write(json.dumps(result, ensure_ascii=False) + "\n")
            append_transcript(transcript_file, result)
            task_info = f" task_id={result.get('task_id')}" if 'task_id' in result else ""
            print(f"[case {i}] method={result['extract_method']} response_len={len(result['response'])}{task_info}")

    print(f"Done. Summary file: {result_file}")
    print(f"Transcript file: {transcript_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
