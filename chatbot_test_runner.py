#!/usr/bin/env python3
"""ADB chatbot test runner.

This script automates a chat-style Android app using ADB and extracts bot replies.
It tries UIAutomator XML extraction first, then falls back to OCR from screenshots.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image
import pytesseract


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


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


DEFAULT_CHATGPT_PACKAGE = "com.openai.chatgpt"
DEFAULT_CHATGPT_PROMPT = "大香蕉"
DEFAULT_CHATGPT_INPUT_POINT = Point(714, 2685)
DEFAULT_CHATGPT_SEND_POINT = Point(1285, 1589)
DEFAULT_CHATGPT_RESPONSE_REGION = Region(0, 370, 1440, 2505)
DEFAULT_CHATGPT_WAIT_SEC = 8.0
ADB_KEYBOARD_IME = "com.android.adbkeyboard/.AdbIME"


class ADBChatbotTester:
    def __init__(
        self,
        device_serial: Optional[str],
        package_name: str,
        activity_name: Optional[str],
        input_point: Point,
        send_point: Point,
        response_region: Region,
        output_dir: Path,
        ocr_lang: str,
    ) -> None:
        self.device_serial = device_serial
        self.package_name = package_name
        self.activity_name = activity_name
        self.input_point = input_point
        self.send_point = send_point
        self.response_region = response_region
        self.output_dir = output_dir
        self.ocr_lang = ocr_lang

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

    def _get_default_input_method(self) -> str:
        result = self._run_adb(["shell", "settings", "get", "secure", "default_input_method"])
        return result.stdout.strip()

    def _list_input_methods(self) -> List[str]:
        result = self._run_adb(["shell", "ime", "list", "-s"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _use_adb_keyboard_for_unicode(self, text: str) -> bool:
        if ADB_KEYBOARD_IME not in self._list_input_methods():
            return False

        previous_ime = self._get_default_input_method()
        try:
            self._run_adb(["shell", "ime", "enable", ADB_KEYBOARD_IME], check=False)
            self._run_adb(["shell", "ime", "set", ADB_KEYBOARD_IME])
            self._run_adb(["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text])
            return True
        finally:
            if previous_ime:
                self._run_adb(["shell", "ime", "set", previous_ime], check=False)

    def input_text(self, text: str) -> None:
        is_ascii_only = all(ord(ch) < 128 for ch in text)

        if is_ascii_only:
            sanitized = text.replace(" ", "%s")
            self._run_adb(["shell", "input", "text", sanitized])
            return

        if self._use_adb_keyboard_for_unicode(text):
            return

        raise RuntimeError(
            "This device cannot input non-ASCII text with the stock adb text command, "
            "and no ADB Keyboard IME was found. Install ADB Keyboard on the device to "
            "send Chinese or other Unicode text automatically."
        )

    def scroll_to_bottom(self) -> None:
        """Fling the chat to the bottom so the latest reply is visible."""
        cx = (self.response_region.left + self.response_region.right) // 2
        cy_top = self.response_region.bottom - 100
        cy_bot = self.response_region.top + 100
        self._run_adb(
            ["shell", "input", "swipe",
             str(cx), str(cy_top), str(cx), str(cy_bot), "300"]
        )
        time.sleep(0.6)

    def send_message(self, message: str, post_send_wait_sec: float) -> None:
        self.tap(self.input_point)
        time.sleep(0.3)
        self.input_text(message)
        time.sleep(0.2)
        self.tap(self.send_point)
        time.sleep(post_send_wait_sec)
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

    def extract_text_from_ui_dump(self, xml_content: str, sent_prompt: str = "") -> List[str]:
        root = ET.fromstring(xml_content)
        collected: List[str] = []
        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            if not text:
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
        return texts

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
        return pytesseract.image_to_string(crop, lang=self.ocr_lang, config=config).strip()

    def run_test_case(self, case_id: int, prompt: str, wait_sec: float) -> dict:
        case_dir = self.output_dir / f"case_{case_id:03d}"
        case_dir.mkdir(parents=True, exist_ok=True)

        self.send_message(prompt, wait_sec)

        screenshot_file = case_dir / "screen.png"
        self.take_screenshot(screenshot_file)

        method = "ui_dump"
        response_text = ""

        try:
            xml_content = self.get_uiautomator_xml()
            ui_texts = self.extract_text_from_ui_dump(xml_content, sent_prompt=prompt)
            response_text = "\n".join(ui_texts).strip()
        except Exception:
            response_text = ""

        if not response_text:
            method = "ocr"
            response_text = self.extract_text_by_ocr(screenshot_file)

        result = {
            "case_id": case_id,
            "prompt": prompt,
            "response": response_text,
            "extract_method": method,
            "screenshot": str(screenshot_file),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

        with (case_dir / "result.json").open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        return result


def append_transcript(transcript_file: Path, result: dict) -> None:
    lines = [
        f"[{result['timestamp']}] CASE {result['case_id']}",
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


def load_prompts(prompt: Optional[str], prompt_file: Optional[Path]) -> List[str]:
    if prompt:
        return [prompt]
    if prompt_file:
        lines = [line.strip() for line in prompt_file.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line]
    raise ValueError("Provide --prompt or --prompt-file")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Automate chatbot app via ADB and extract replies")
    p.add_argument("--device-serial", default=None, help="ADB device serial")
    p.add_argument("--package", default=None, help="App package name, e.g. com.example.chat")
    p.add_argument("--activity", default=None, help="Activity name, e.g. .MainActivity")
    p.add_argument("--prompt", default=None, help="Single prompt to send")
    p.add_argument("--prompt-file", type=Path, default=None, help="Text file of prompts (one line per prompt)")
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
    p.epilog = (
        "Examples:\n"
        "  python chatbot_test_runner.py --chatgpt-auto-once\n"
        "  python chatbot_test_runner.py --package com.openai.chatgpt --prompt \"Hello\" "
        "--input-point 714,2685 --send-point 1285,1589 --response-region 0,370,1440,2505"
    )
    return p


def validate_required_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    missing = []
    if not args.package:
        missing.append("--package")
    if not args.input_point:
        missing.append("--input-point")
    if not args.send_point:
        missing.append("--send-point")
    if not args.response_region:
        missing.append("--response-region")
    if missing:
        parser.error(
            "Missing required arguments: "
            + ", ".join(missing)
            + ". Use --chatgpt-auto-once for the built-in ChatGPT preset."
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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.chatgpt_auto_once or len(sys.argv) == 1:
        args = apply_chatgpt_auto_defaults(args)
        prompts = [args.prompt]
    else:
        validate_required_args(args, parser)
        prompts = load_prompts(args.prompt, args.prompt_file)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    tester = ADBChatbotTester(
        device_serial=args.device_serial,
        package_name=args.package,
        activity_name=args.activity,
        input_point=parse_point(args.input_point),
        send_point=parse_point(args.send_point),
        response_region=parse_region(args.response_region),
        output_dir=args.output_dir,
        ocr_lang=args.ocr_lang,
    )

    tester.ensure_device_ready()
    tester.launch_app()
    time.sleep(1.5)

    result_file = args.output_dir / "summary.jsonl"
    transcript_file = args.output_dir / f"{args.session_name}_transcript.txt"
    with result_file.open("a", encoding="utf-8") as summary:
        for i, prompt in enumerate(prompts, start=1):
            result = tester.run_test_case(i, prompt, args.wait_sec)
            summary.write(json.dumps(result, ensure_ascii=False) + "\n")
            append_transcript(transcript_file, result)
            print(f"[case {i}] method={result['extract_method']} response_len={len(result['response'])}")

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
