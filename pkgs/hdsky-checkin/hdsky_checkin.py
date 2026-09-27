#!/usr/bin/env python3
"""HDSky 自动签到。

签到流程（站点三步，2024-04 抓包所得）：
  1. POST /image_code_ajax.php  action=new            -> {success, code=imagehash}
  2. GET  /image.php?action=regimage&imagehash=<code>  -> 验证码图片
  3. POST /showup.php  action=showup&imagehash&imagestring -> 签到结果
     success=true               签到成功
     message="date_unmatch"     今日已签过（当成功处理）

设计要点：
  * Cookie 从文件读（sops-nix 渲染），以原始 Cookie 请求头发出去，与浏览器逐字节一致；
    不走 requests 的 cookies dict，避免 %XX 被二次转义。
  * TLS 校验保持开启——cookie 就是登录凭据，不能为了省事关掉校验。
  * 验证码识别错就重取一张重试，有次数上限，绝不无限循环（cron 场景）。
  * 任何失败（cookie 失效 / 接口异常 / 验证码耗尽）都发一封告警邮件到本机 sendmail，
    收件人由 --mail-to-file 指定（同样来自 sops）。不给该参数则不告警。
  * 退出码：0 成功/今日已签，2 配置缺失，3 接口异常，4 重试耗尽。
"""
import argparse
import logging
import subprocess
import sys
from email.message import EmailMessage
from urllib.parse import urlencode

import requests
from ddddocr import DdddOcr

log = logging.getLogger("hdsky-checkin")

SITE = "https://hdsky.me"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


class ApiError(Exception):
    """站点返回了非预期结构 / 非 200。"""


def read_file(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError as exc:
        raise SystemExit(f"cannot read {what} file {path}: {exc}")
    if not value:
        raise SystemExit(f"{what} file is empty: {path}")
    return value


def send_alert(sender, recipient, subject, body, sendmail_bin):
    """经本机 sendmail（postfix relay）发一封告警信。失败只记日志，不改变退出码。"""
    if not recipient:
        return
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        result = subprocess.run(
            [sendmail_bin, "-f", sender, recipient],
            input=msg.as_bytes(),
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("sendmail exited %d: %s", result.returncode, result.stderr.decode(errors="replace"))
        else:
            log.info("告警邮件已发给 %s", recipient)
    except Exception as exc:  # noqa: BLE001 - 发信失败不能反过来污染签到结论
        log.error("发送告警邮件失败: %s", exc)


def build_session(user_agent, cookie):
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        # 站点用 x-www-form-urlencoded 收表单
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        # 整条 Cookie 原样转发，等价于浏览器里复制出来的请求头
        "Cookie": cookie,
    })
    return s


def fetch_imagehash(session, timeout):
    payload = urlencode({"action": "new"})
    r = session.post(f"{SITE}/image_code_ajax.php", data=payload, timeout=timeout)
    if r.status_code != 200:
        raise ApiError(f"image_code_ajax.php http {r.status_code}")
    body = r.json()
    if not body.get("success") or not body.get("code"):
        raise ApiError(f"image_code_ajax.php unexpected: {body}")
    return body["code"]


def fetch_captcha(session, imagehash, timeout):
    r = session.get(
        f"{SITE}/image.php",
        params={"action": "regimage", "imagehash": imagehash},
        timeout=timeout,
        stream=True,
    )
    if r.status_code != 200:
        raise ApiError(f"image.php http {r.status_code}")
    if not r.content:
        raise ApiError("image.php returned empty body")
    return r.content


def submit(session, imagehash, imagestring, timeout):
    payload = {
        "action": "showup",
        "imagehash": imagehash,
        "imagestring": imagestring,
    }
    r = session.post(f"{SITE}/showup.php", data=payload, timeout=timeout)
    if r.status_code != 200:
        raise ApiError(f"showup.php http {r.status_code}")
    return r.json()


def run(args):
    """返回 (退出码, 失败原因)。失败原因为 None 表示成功。"""
    cookie = read_file(args.cookie_file, "cookie")
    session = build_session(args.user_agent, cookie)

    try:
        ocr = DdddOcr()
    except Exception as exc:  # noqa: BLE001
        log.error("OCR 引擎初始化失败: %s", exc)
        return 3, f"OCR 引擎初始化失败: {exc}"

    for attempt in range(1, args.max_attempts + 1):
        try:
            imagehash = fetch_imagehash(session, args.timeout)
        except (ApiError, ValueError) as exc:
            log.error("取 imagehash 失败: %s", exc)
            return 3, f"取 imagehash 失败（多为 Cookie 失效或站点改版）: {exc}"

        try:
            image = fetch_captcha(session, imagehash, args.timeout)
        except ApiError as exc:
            log.error("取验证码图片失败: %s", exc)
            return 3, f"取验证码图片失败: {exc}"

        try:
            answer = ocr.classification(image)
        except Exception as exc:  # noqa: BLE001 - OCR 引擎异常不该杀掉整个任务
            log.error("OCR 识别异常: %s", exc)
            answer = ""
        log.info("第 %d/%d 次验证码识别结果: %r", attempt, args.max_attempts, answer)

        if not answer:
            continue

        try:
            result = submit(session, imagehash, answer, args.timeout)
        except (ApiError, ValueError) as exc:
            log.error("提交签到失败: %s", exc)
            return 3, f"提交签到失败: {exc}"

        if result.get("success"):
            log.info("签到成功")
            return 0, None

        message = result.get("message", "")
        if message == "date_unmatch":
            log.info("今日已签到过（date_unmatch），无需重复")
            return 0, None

        log.warning("签到未通过（message=%s），换一张验证码重试", message)

    log.error("重试 %d 次仍未成功，放弃", args.max_attempts)
    return 4, f"验证码重试 {args.max_attempts} 次仍未通过"


def main():
    parser = argparse.ArgumentParser(description="HDSky 自动签到")
    parser.add_argument("--cookie-file", required=True,
                        help="Cookie 文本文件（一行 k=v; k=v; ...，由 sops-nix 渲染）")
    parser.add_argument("--user-agent", default=DEFAULT_UA,
                        help="请求 User-Agent（默认 Safari macOS，与取 cookie 的浏览器一致）")
    parser.add_argument("--max-attempts", type=int, default=8,
                        help="验证码重试上限（默认 8）")
    parser.add_argument("--timeout", type=int, default=20,
                        help="HTTP 超时秒数（默认 20）")
    parser.add_argument("--mail-to-file", default="",
                        help="告警收件人文件（sops-nix 渲染）。不给则不发告警。")
    parser.add_argument("--mail-from", default="",
                        help="告警发件人（须与 postfix relay 账号一致，供 generic map 重写）")
    parser.add_argument("--sendmail", default="/run/wrappers/bin/sendmail",
                        help="sendmail 路径")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s - %(message)s",
    )

    code, reason = run(args)

    if code != 0:
        mail_to = ""
        if args.mail_to_file:
            try:
                mail_to = read_file(args.mail_to_file, "mail-to")
            except SystemExit as exc:
                log.error("%s", exc)
        if mail_to and args.mail_from:
            send_alert(
                sender=args.mail_from,
                recipient=mail_to,
                subject=f"[hdsky-checkin] 签到失败（退出码 {code}）",
                body=(
                    f"HDSky（{SITE}）自动签到失败。\n\n"
                    f"原因：{reason}\n"
                    f"退出码：{code}\n\n"
                    f"常见处理：Cookie 过期就从 Safari 重新取一份，"
                    f"更新到 sops 的 secrets/hdsky_cookie 后重启服务。\n"
                ),
                sendmail_bin=args.sendmail,
            )

    sys.exit(code)


if __name__ == "__main__":
    main()
