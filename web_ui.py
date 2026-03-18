"""Log-based replay viewer for the Three Kingdoms Werewolf game."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = "game_log.txt"
LOG_SEARCH_DIRS = (PROJECT_ROOT, PROJECT_ROOT / "exports")
PLAYER_COLORS = [
    "#c76a4b",
    "#4f7c82",
    "#8b5cf6",
    "#d97706",
    "#059669",
    "#dc2626",
    "#2563eb",
    "#7c3aed",
    "#ca8a04",
]
ROLE_COLORS = {
    "狼人": "#a11d33",
    "预言家": "#2563eb",
    "女巫": "#7c3aed",
    "猎人": "#a16207",
    "守护者": "#0f766e",
    "村民": "#475569",
}
EVENT_LABELS = {
    "setup": "入场",
    "round": "回合开始",
    "phase": "阶段切换",
    "speech": "玩家发言",
    "attack": "狼人行动",
    "inspect": "查验结果",
    "save": "女巫救人",
    "poison": "女巫毒杀",
    "death": "死亡结算",
    "vote": "投票结果",
    "result": "夜晚结果",
    "ending": "终局",
    "narration": "旁白",
}


@dataclass
class ReplayData:
    source_file: str
    players: list[dict[str, Any]]
    events: list[dict[str, Any]]
    raw_text: str


def discover_log_files() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    for directory in LOG_SEARCH_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.txt")):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            stat = path.stat()
            files.append(
                {
                    "id": rel,
                    "name": path.name,
                    "size": stat.st_size,
                    "updated_at": stat.st_mtime,
                }
            )

    files.sort(key=lambda item: item["updated_at"], reverse=True)
    return files


def resolve_log_file(file_id: str | None) -> Path:
    candidates = {item["id"]: PROJECT_ROOT / item["id"] for item in discover_log_files()}

    if file_id and file_id in candidates:
        return candidates[file_id]

    default_path = PROJECT_ROOT / DEFAULT_LOG_FILE
    if default_path.exists():
        return default_path

    if candidates:
        return next(iter(candidates.values()))

    raise FileNotFoundError("未找到可用的日志文件")


def normalize_line(line: str) -> str:
    return line.replace("\ufeff", "").strip()


def build_player(name: str, role: str | None, index: int) -> dict[str, Any]:
    role = role or "未知"
    return {
        "name": name,
        "role": role,
        "alive": True,
        "status_text": "存活",
        "color": ROLE_COLORS.get(role, PLAYER_COLORS[index % len(PLAYER_COLORS)]),
    }


def snapshot_players(players: dict[str, dict[str, Any]], order: list[str]) -> list[dict[str, Any]]:
    return [dict(players[name]) for name in order if name in players]


def mark_dead(players: dict[str, dict[str, Any]], names: list[str]) -> None:
    for name in names:
        if name not in players:
            continue
        players[name]["alive"] = False
        players[name]["status_text"] = "出局"


def parse_names(text: str) -> list[str]:
    return [item.strip() for item in text.split("、") if item.strip()]


def is_noise_line(line: str) -> bool:
    lowered = line.lower()
    if not line:
        return True
    if line.startswith("system:"):
        return True
    if "arguments validation error" in lowered:
        return True
    if line in {"{", "}", "[", "]"}:
        return True
    if line.startswith('"') or line.startswith("'"):
        return True
    if line.startswith('"type"') or line.startswith('"id"') or line.startswith('"name"'):
        return True
    if line.startswith('"output"') or line.startswith('"text"'):
        return True
    return False


def clean_speech_content(content: str) -> str | None:
    content = content.strip()
    if not content:
        return None
    if content.startswith("{") or content.startswith("["):
        return None
    if '"reach_agreement"' in content or '"vote"' in content or '"target"' in content:
        return None
    return content.replace("\\n", " ").strip()


def should_skip_plain_line(line: str) -> bool:
    return (
        "欢迎来到三国狼人杀" in line
        or "开始设置三国狼人杀游戏" in line
        or "游戏设置完成" in line
    )


def append_event(
    events: list[dict[str, Any]],
    players: dict[str, dict[str, Any]],
    order: list[str],
    *,
    event_type: str,
    text: str,
    round_num: int | None,
    phase: str,
    speaker: str | None = None,
    focus_players: list[str] | None = None,
    actor: str | None = None,
    actor_role: str | None = None,
    target: str | None = None,
) -> None:
    events.append(
        {
            "id": len(events) + 1,
            "type": event_type,
            "text": text,
            "speaker": speaker,
            "round": round_num,
            "phase": phase,
            "focus_players": focus_players or [],
            "actor": actor,
            "actor_role": actor_role,
            "target": target,
            "players": snapshot_players(players, order),
        }
    )


def parse_game_log(file_path: Path) -> ReplayData:
    raw_text = file_path.read_text(encoding="utf-8", errors="replace")
    lines = [normalize_line(line) for line in raw_text.splitlines()]

    player_order: list[str] = []
    player_map: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    round_num: int | None = None
    phase = "序章"

    role_pattern = re.compile(r"【(?P<name>[^】]+)】你在这场三国狼人杀中扮演(?P<role>[^，。]+)")
    round_pattern = re.compile(r"第(?P<round>\d+)轮游戏开始")
    night_pattern = re.compile(r"第(?P<round>\d+)夜降临")
    day_pattern = re.compile(r"第(?P<round>\d+)天天亮了")
    vote_pattern = re.compile(r"投票结果：(?P<name>[^以]+)以(?P<count>\d+)票被淘汰出局")
    inspect_pattern = re.compile(r"查验结果：(?P<name>[^是]+)是(?P<camp>狼人|好人)")
    attack_pattern = re.compile(r"今晚(?P<name>[^被]+)被狼人击杀")
    antidote_pattern = re.compile(r"你使用解药救了(?P<name>.+)")
    poison_pattern = re.compile(r"你使用毒药毒杀了(?P<name>.+)")
    death_pattern = re.compile(r"昨夜，(?P<names>.+)不幸遇害")

    for line in lines:
        if is_noise_line(line):
            continue
        if should_skip_plain_line(line):
            continue

        match = role_pattern.search(line)
        if match:
            name = match.group("name")
            role = match.group("role")
            if name not in player_map:
                player_order.append(name)
                player_map[name] = build_player(name, role, len(player_order) - 1)
            else:
                player_map[name]["role"] = role
                player_map[name]["color"] = ROLE_COLORS.get(role, player_map[name]["color"])
            continue

        if "参与者：" in line:
            for name in parse_names(line.split("参与者：", 1)[1]):
                if name not in player_map:
                    player_order.append(name)
                    player_map[name] = build_player(name, None, len(player_order) - 1)
            append_event(events, player_map, player_order, event_type="setup", text="玩家入场，围桌落座。", round_num=round_num, phase=phase, focus_players=list(player_order))
            continue

        match = round_pattern.search(line)
        if match:
            round_num = int(match.group("round"))
            phase = "回合开始"
            append_event(events, player_map, player_order, event_type="round", text=f"第 {round_num} 轮开始", round_num=round_num, phase=phase)
            continue

        match = night_pattern.search(line)
        if match:
            round_num = int(match.group("round"))
            phase = "夜晚"
            append_event(events, player_map, player_order, event_type="phase", text=f"第 {round_num} 夜，天黑请闭眼。", round_num=round_num, phase=phase)
            continue

        match = day_pattern.search(line)
        if match:
            round_num = int(match.group("round"))
            phase = "白天"
            append_event(events, player_map, player_order, event_type="phase", text=f"第 {round_num} 天，太阳升起。", round_num=round_num, phase=phase)
            continue

        if "昨夜平安无事" in line:
            append_event(events, player_map, player_order, event_type="result", text="昨夜平安无事，无人出局。", round_num=round_num, phase=phase)
            continue

        match = death_pattern.search(line)
        if match:
            names = parse_names(match.group("names"))
            mark_dead(player_map, names)
            append_event(events, player_map, player_order, event_type="death", text=f"{'、'.join(names)} 在昨夜出局。", round_num=round_num, phase=phase, focus_players=names)
            continue

        match = vote_pattern.search(line)
        if match:
            name = match.group("name").strip()
            count = match.group("count").strip()
            mark_dead(player_map, [name])
            append_event(events, player_map, player_order, event_type="vote", text=f"{name} 被投票淘汰，票数 {count}。", round_num=round_num, phase=phase, focus_players=[name])
            continue

        match = inspect_pattern.search(line)
        if match:
            target = match.group("name").strip()
            camp = match.group("camp").strip()
            append_event(events, player_map, player_order, event_type="inspect", text=f"预言家查验了 {target}，结果是{camp}。", round_num=round_num, phase=phase, focus_players=[target], actor_role="预言家", target=target)
            continue

        match = attack_pattern.search(line)
        if match:
            target = match.group("name").strip()
            append_event(events, player_map, player_order, event_type="attack", text=f"狼人将目标锁定为 {target}。", round_num=round_num, phase=phase, focus_players=[target], actor_role="狼人", target=target)
            continue

        match = antidote_pattern.search(line)
        if match:
            target = match.group("name").strip()
            append_event(events, player_map, player_order, event_type="save", text=f"女巫使用解药救下了 {target}。", round_num=round_num, phase=phase, focus_players=[target], actor_role="女巫", target=target)
            continue

        match = poison_pattern.search(line)
        if match:
            target = match.group("name").strip()
            mark_dead(player_map, [target])
            append_event(events, player_map, player_order, event_type="poison", text=f"女巫使用毒药带走了 {target}。", round_num=round_num, phase=phase, focus_players=[target], actor_role="女巫", target=target)
            continue

        if "游戏结束" in line:
            append_event(events, player_map, player_order, event_type="ending", text=line.replace("游戏主持人: 📢 ", "").replace("🎉 ", ""), round_num=round_num, phase=phase)
            continue

        if ":" in line:
            speaker, content = line.split(":", 1)
            speaker = speaker.strip()
            cleaned = clean_speech_content(content)
            if cleaned is None and (speaker in player_map or speaker in {"system", "游戏主持人"}):
                continue
            if cleaned and speaker in player_map:
                append_event(events, player_map, player_order, event_type="speech", text=cleaned, round_num=round_num, phase=phase, speaker=speaker, focus_players=[speaker], actor=speaker)
                continue
            if cleaned and speaker == "游戏主持人":
                append_event(events, player_map, player_order, event_type="narration", text=cleaned.replace("📢 ", ""), round_num=round_num, phase=phase)
                continue

        append_event(events, player_map, player_order, event_type="narration", text=line, round_num=round_num, phase=phase)

    if not player_order:
        raise ValueError("日志中未识别到玩家信息，无法生成回放")

    if not events:
        append_event(events, player_map, player_order, event_type="setup", text="日志已加载，但没有识别到可回放事件。", round_num=round_num, phase=phase)

    return ReplayData(source_file=file_path.relative_to(PROJECT_ROOT).as_posix(), players=snapshot_players(player_map, player_order), events=events, raw_text=raw_text)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>三国狼人杀日志回放</title>
  <style>
    :root{--bg-top:#efe6d5;--bg-bottom:#d7c0a2;--panel:rgba(255,248,236,.88);--panel-border:rgba(110,74,45,.16);--ink:#2f231c;--muted:#775f50;--dead:#84756a}
    *{box-sizing:border-box}
    body{margin:0;min-height:100vh;font-family:"Microsoft YaHei UI","PingFang SC",sans-serif;color:var(--ink);background:radial-gradient(circle at 15% 20%,rgba(255,255,255,.4),transparent 24%),radial-gradient(circle at 85% 15%,rgba(255,240,211,.7),transparent 26%),linear-gradient(180deg,var(--bg-top),var(--bg-bottom))}
    .page{padding:18px;display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}
    .panel{background:var(--panel);border:1px solid var(--panel-border);border-radius:24px;box-shadow:0 18px 40px rgba(95,66,39,.08);backdrop-filter:blur(10px)}
    .sidebar{padding:18px;display:flex;flex-direction:column;gap:16px}
    .brand{padding:18px;background:linear-gradient(135deg,#6d271d,#a4452d);color:#fdf4e8;border-radius:20px;box-shadow:0 14px 26px rgba(112,44,29,.22)}
    .brand h1{margin:0 0 8px;font-size:26px}.brand p{margin:0;color:#f3d8bf;line-height:1.6;font-size:14px}
    .card{padding:16px;border-radius:20px;background:rgba(255,255,255,.42);border:1px solid rgba(112,80,51,.1)}
    .card h2{margin:0 0 12px;font-size:17px}.label{display:block;font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:700}
    select,button,input[type=range]{width:100%}
    select,button{border-radius:14px;border:1px solid rgba(124,84,52,.18);font-size:14px;padding:12px 14px}
    select{background:rgba(255,255,255,.74);margin-bottom:12px}
    button{cursor:pointer;font-weight:700;margin-bottom:10px;transition:transform .16s ease,box-shadow .16s ease}
    button:hover{transform:translateY(-1px);box-shadow:0 12px 20px rgba(92,59,37,.12)}
    button.primary{background:linear-gradient(135deg,#8d3122,#bb5b39);color:#fff7ec;border-color:transparent}
    button.secondary{background:rgba(255,255,255,.78);color:var(--ink)}
    .inline{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .speed{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px}.speed strong{min-width:44px;text-align:right;color:var(--ink)}
    .main{padding:18px;display:grid;grid-template-rows:minmax(520px,70vh) auto;gap:16px}
    .scene{position:relative;overflow:hidden;border-radius:26px;background:radial-gradient(circle at 50% 18%,rgba(255,243,223,.75),transparent 22%),linear-gradient(180deg,#f3ead8 0%,#ddc7a7 58%,#b58f62 100%);box-shadow:inset 0 -30px 80px rgba(86,49,23,.12);transition:background .45s ease,box-shadow .45s ease,filter .45s ease}
    .scene:before{content:"";position:absolute;inset:auto 0 0 0;height:34%;background:radial-gradient(circle at 50% 10%,rgba(58,41,28,.22),transparent 44%),linear-gradient(180deg,rgba(117,78,42,.12),rgba(94,61,34,.42));transition:background .45s ease,opacity .45s ease}
    .scene.scene-day{background:rgba(246,235,206,.92);box-shadow:inset 0 -18px 40px rgba(133,92,45,.05)}
    .scene.scene-day:before{background:none;opacity:0}
    .scene.scene-night{background:radial-gradient(circle at 50% 15%,rgba(124,152,210,.28),transparent 24%),linear-gradient(180deg,#1d2744 0%,#2f3f5e 48%,#5e4c5f 100%);box-shadow:inset 0 -34px 88px rgba(7,12,27,.34)}
    .scene.scene-night:before{background:radial-gradient(circle at 50% 8%,rgba(164,185,255,.1),transparent 44%),linear-gradient(180deg,rgba(29,40,65,.08),rgba(15,19,34,.46))}
    .room-glow{position:absolute;inset:0;background:radial-gradient(circle at 50% 28%,rgba(255,236,194,.46),transparent 24%),radial-gradient(circle at 50% 32%,rgba(255,255,255,.16),transparent 14%);pointer-events:none;transition:background .45s ease,opacity .45s ease}
    .scene.scene-day .room-glow{background:none;opacity:0}
    .scene.scene-night .room-glow{background:radial-gradient(circle at 50% 22%,rgba(158,183,255,.24),transparent 22%),radial-gradient(circle at 50% 30%,rgba(228,239,255,.1),transparent 12%);opacity:.9}
    .table{position:absolute;width:min(44vw,500px);height:min(20vw,218px);left:50%;top:59.5%;transform:translate(-50%,-50%);border-radius:50%;background:radial-gradient(circle at 50% 38%,#b6784a 0%,#98613c 45%,#744727 78%,#55321d 100%);box-shadow:0 26px 46px rgba(72,40,19,.24),inset 0 3px 14px rgba(255,236,204,.16),inset 0 -10px 24px rgba(60,34,19,.2);transition:background .45s ease,box-shadow .45s ease}
    .scene.scene-night .table{background:radial-gradient(circle at 50% 40%,#87635b 0%,#6a4a44 46%,#4b3134 78%,#332027 100%);box-shadow:0 24px 44px rgba(8,10,20,.4),inset 0 4px 16px rgba(222,230,255,.08)}
    .table:before{content:"";position:absolute;inset:12% 10%;border-radius:50%;border:1px solid rgba(255,233,204,.14)}
    .player{position:absolute;width:108px;height:162px;transform:translate(-50%,-50%);transition:left .6s ease,top .6s ease,opacity .4s ease,filter .4s ease;animation:floaty 3.6s ease-in-out infinite}
    .player.is-current{z-index:3}.player.is-current .name-tag{transform:translateY(-4px);box-shadow:0 14px 24px rgba(111,54,31,.18)}
    .player.is-target .halo,.player.is-current .halo{opacity:1;transform:translate(-50%,-50%) scale(1.04)}
    .player.is-target .name-tag{box-shadow:0 14px 24px rgba(161,29,51,.22)}
    .player.is-acting .turn-arrow{opacity:1;transform:translateY(-50%) translateX(0)}
    .player.is-focused .halo{opacity:1;transform:translate(-50%,-50%) scale(1.08)}
    .player.is-dead{opacity:.38;filter:grayscale(1)}.player.is-dead .avatar-shell{transform:rotate(-12deg) translateY(8px)}
    .player.is-dead .dead-mark{opacity:1;transform:scale(1)}
    .halo{position:absolute;width:122px;height:122px;left:50%;top:58px;transform:translate(-50%,-50%) scale(.8);border-radius:50%;background:radial-gradient(circle,rgba(255,212,143,.45),transparent 62%);opacity:0;transition:opacity .25s ease,transform .25s ease}
    .avatar-shell{position:absolute;left:50%;top:50%;width:100px;height:142px;transform:translate(-50%,-50%);transition:transform .3s ease}
    .dead-mark{position:absolute;right:6px;top:8px;width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(122,28,28,.9);color:#fff;font-size:18px;box-shadow:0 10px 18px rgba(70,18,18,.22);opacity:0;transform:scale(.7);transition:opacity .22s ease,transform .22s ease;z-index:4}
    .turn-arrow{position:absolute;right:-20px;top:56px;transform:translateY(-50%) translateX(-8px);width:34px;height:34px;border-radius:12px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#ffd55c,#ffb703);box-shadow:0 10px 18px rgba(121,78,20,.22);font-size:18px;opacity:0;transition:opacity .2s ease,transform .2s ease;z-index:5}
    .head{position:absolute;width:42px;height:42px;left:50%;top:6px;transform:translateX(-50%);border-radius:50%;background:linear-gradient(180deg,#f4d4b7,#d8a785);box-shadow:inset 0 -4px 10px rgba(145,89,59,.18);z-index:2;overflow:hidden}
    .head:after{content:"";position:absolute;left:8px;top:6px;width:18px;height:10px;border-radius:50%;background:rgba(255,255,255,.14);filter:blur(1px)}
    .brow{position:absolute;top:12px;width:10px;height:2px;border-radius:999px;background:#4a3026;transition:all .2s ease}
    .brow.left{left:9px;transform:rotate(8deg)}.brow.right{right:9px;transform:rotate(-8deg)}
    .eye{position:absolute;top:18px;width:8px;height:9px;border-radius:50%;background:#fff;box-shadow:0 1px 0 rgba(61,39,31,.12);transition:all .2s ease}
    .eye.left{left:10px}.eye.right{right:10px}
    .eye:after{content:"";position:absolute;left:2px;top:2px;width:4px;height:5px;border-radius:50%;background:#3d251a;transition:all .2s ease}
    .blush{position:absolute;top:24px;width:9px;height:5px;border-radius:999px;background:rgba(223,121,121,.28);filter:blur(.4px);transition:opacity .2s ease}
    .blush.left{left:5px}.blush.right{right:5px}
    .mouth{position:absolute;left:50%;top:28px;transform:translateX(-50%);width:10px;height:5px;border:2px solid #8a4e43;border-top:none;border-radius:0 0 10px 10px;transition:all .2s ease}
    .player.is-asleep .brow{top:15px;background:#5f4337}
    .player.is-asleep .eye{height:2px;top:21px;border-radius:999px;background:#5f4337;box-shadow:none}
    .player.is-asleep .eye:after{opacity:0}
    .player.is-asleep .mouth{width:8px;height:2px;top:29px;border:none;background:#8a4e43;border-radius:999px}
    .player.expression-neutral .mouth{width:10px;height:4px}
    .player.expression-speaking .brow.left{transform:rotate(14deg)}.player.expression-speaking .brow.right{transform:rotate(-14deg)}
    .player.expression-speaking .mouth{width:8px;height:8px;top:27px;border:2px solid #8a4e43;border-radius:50%;background:rgba(146,79,73,.12);animation:talk 0.42s ease-in-out infinite}
    .player.expression-speaking .eye{height:10px}
    .player.expression-acting .brow.left{transform:rotate(-10deg)}.player.expression-acting .brow.right{transform:rotate(10deg)}
    .player.expression-acting .mouth{width:12px;height:3px;top:29px;border-width:2px}
    .player.expression-target .brow.left{transform:rotate(18deg)}.player.expression-target .brow.right{transform:rotate(-18deg)}
    .player.expression-target .mouth{width:9px;height:4px;top:30px;border-top:2px solid #8a4e43;border-right:none;border-bottom:none;border-left:none;border-radius:10px 10px 0 0}
    .player.expression-target .eye{top:19px}
    .player.expression-happy .mouth{width:12px;height:6px;top:28px}
    .player.expression-happy .blush{opacity:.9}
    .player.expression-focused .brow.left{transform:rotate(-2deg)}.player.expression-focused .brow.right{transform:rotate(2deg)}
    .player.is-dead .mouth,.player.is-dead .brow,.player.is-dead .eye,.player.is-dead .blush{opacity:.5}
    .hair{position:absolute;width:50px;height:22px;left:50%;top:2px;transform:translateX(-50%);border-radius:24px 24px 14px 14px;background:linear-gradient(180deg,#34221c,#513227);z-index:3}
    .body{position:absolute;width:58px;height:68px;left:50%;top:42px;transform:translateX(-50%);border-radius:26px 26px 18px 18px;background:var(--robe);box-shadow:inset 0 -10px 18px rgba(28,21,18,.18)}
    .body:before{content:"";position:absolute;inset:0;border-radius:inherit;background:linear-gradient(180deg,rgba(255,255,255,.2),transparent 42%)}
    .arm{position:absolute;width:16px;height:58px;top:48px;border-radius:14px;background:var(--robe);opacity:.92}.arm.left{left:16px;transform:rotate(18deg)}.arm.right{right:16px;transform:rotate(-18deg)}
    .leg{position:absolute;width:18px;height:42px;bottom:2px;border-radius:12px;background:#5b3a2a}.leg.left{left:32px}.leg.right{right:32px}
    .name-tag{position:absolute;left:50%;bottom:0;transform:translateX(-50%);min-width:88px;padding:8px 10px;border-radius:999px;text-align:center;color:#fff7ec;background:rgba(62,35,22,.88)}
    .name-tag strong{display:block;font-size:13px}.name-tag span{display:block;margin-top:2px;font-size:11px;color:rgba(255,239,217,.82)}
    .bubble{position:absolute;left:50%;top:18px;transform:translateX(-50%);width:min(760px,calc(100% - 40px));min-height:96px;padding:18px 22px;border-radius:24px;background:rgba(255,251,242,.9);border:1px solid rgba(120,86,56,.16);box-shadow:0 18px 36px rgba(87,57,31,.12);transition:left .28s ease,top .28s ease,width .2s ease,transform .2s ease,background .35s ease}
    .scene.scene-night .bubble{background:rgba(244,246,255,.9)}
    .bubble:after{display:none;content:"";position:absolute;width:26px;height:26px;background:inherit}
    .bubble.is-player{width:min(320px,calc(100% - 36px));min-height:0;padding:16px 18px;transform:none}
    .bubble.is-player:after{display:block}
    .bubble.is-player[data-anchor="bottom"]:after{left:50%;bottom:-14px;transform:translateX(-50%) rotate(45deg);border-right:1px solid rgba(120,86,56,.16);border-bottom:1px solid rgba(120,86,56,.16)}
    .bubble.is-player[data-anchor="top"]:after{left:50%;top:-14px;transform:translateX(-50%) rotate(45deg);border-left:1px solid rgba(120,86,56,.16);border-top:1px solid rgba(120,86,56,.16)}
    .bubble.is-player[data-anchor="left"]:after{left:-14px;top:50%;transform:translateY(-50%) rotate(45deg);border-left:1px solid rgba(120,86,56,.16);border-bottom:1px solid rgba(120,86,56,.16)}
    .bubble.is-player[data-anchor="right"]:after{right:-14px;top:50%;transform:translateY(-50%) rotate(45deg);border-right:1px solid rgba(120,86,56,.16);border-top:1px solid rgba(120,86,56,.16)}
    .bubble small{display:block;color:var(--muted);font-size:12px;letter-spacing:.6px;margin-bottom:8px;text-transform:uppercase}.bubble strong{display:block;font-size:18px;margin-bottom:6px}.bubble p{margin:0;line-height:1.75;font-size:15px;color:#43332a}
    .action-overlay,.action-badges{position:absolute;inset:0;pointer-events:none}
    .action-line{fill:none;stroke:#8d3122;stroke-width:3;stroke-dasharray:8 8;stroke-linecap:round;opacity:.95;filter:drop-shadow(0 6px 10px rgba(76,43,24,.18))}
    .scene.scene-night .action-line{stroke:#d7b79a}
    .action-badge{position:absolute;transform:translate(-50%,-50%);width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:rgba(255,251,242,.94);box-shadow:0 10px 20px rgba(76,43,24,.14);font-size:18px}
    .footer{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px}
    .timeline,.feed{padding:18px}.timeline-header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.timeline-header h2,.feed h2{margin:0;font-size:18px}
    .progress-meta{color:var(--muted);font-size:13px}.range-row{display:grid;grid-template-columns:minmax(0,1fr) 72px;gap:12px;align-items:center;margin-bottom:14px}
    .counter{padding:10px 12px;border-radius:12px;text-align:center;background:rgba(255,255,255,.64);border:1px solid rgba(112,80,51,.12);font-weight:700}
    .chips{display:flex;gap:8px;flex-wrap:wrap}.chip{padding:8px 10px;border-radius:999px;background:rgba(255,255,255,.7);border:1px solid rgba(112,80,51,.1);font-size:12px;color:var(--muted)}
    .feed-list{margin-top:12px;display:flex;flex-direction:column;gap:10px;max-height:230px;overflow:auto;padding-right:4px}
    .feed-item{padding:10px 12px;border-radius:14px;background:rgba(255,255,255,.6);border:1px solid rgba(112,80,51,.08)}
    .feed-item.active{background:linear-gradient(135deg,rgba(163,74,42,.14),rgba(219,176,124,.22));border-color:rgba(143,63,45,.2)}
    .feed-item small{display:block;color:var(--muted);margin-bottom:6px}.feed-item strong{display:block;margin-bottom:4px;font-size:14px}.feed-item p{margin:0;line-height:1.6;color:#48372c;font-size:13px}
    @keyframes floaty{0%,100%{transform:translate(-50%,-50%) translateY(0)}50%{transform:translate(-50%,-50%) translateY(-5px)}}
    @keyframes talk{0%,100%{transform:translateX(-50%) scaleY(1)}50%{transform:translateX(-50%) scaleY(1.22)}}
    @media (max-width:1080px){.page{grid-template-columns:1fr}.main{grid-template-rows:minmax(480px,58vh) auto}.footer{grid-template-columns:1fr}.table{width:min(74vw,500px);height:min(34vw,218px)}}
    @media (max-width:720px){.page{padding:12px}.scene{min-height:560px}.bubble{width:calc(100% - 24px);top:12px;padding:16px 18px}.player{width:92px}.name-tag{min-width:78px}}
  </style>
</head>
<body>
  <main class="page">
    <aside class="panel sidebar">
      <section class="brand">
        <h1>三国狼人杀回放</h1>
        <p>根据终端保存的日志文件，自动生成一个围桌式、AI 小镇感的轻动画前端。</p>
      </section>
      <section class="card">
        <h2>日志源</h2>
        <label class="label" for="file-select">选择日志文件</label>
        <select id="file-select"></select>
        <button class="primary" id="load-btn">加载回放</button>
      </section>
      <section class="card">
        <h2>播放控制</h2>
        <div class="inline">
          <button class="primary" id="play-btn">播放</button>
          <button class="secondary" id="pause-btn">暂停</button>
        </div>
        <div class="inline">
          <button class="secondary" id="prev-btn">上一步</button>
          <button class="secondary" id="next-btn">下一步</button>
        </div>
        <label class="label" for="speed-range">速度</label>
        <div class="speed">
          <input id="speed-range" type="range" min="0.5" max="3" step="0.5" value="1.5">
          <strong id="speed-text">1.5x</strong>
        </div>
      </section>
      <section class="card">
        <h2>当前事件</h2>
        <div class="chips">
          <span class="chip" id="chip-file">文件: -</span>
          <span class="chip" id="chip-round">回合: -</span>
          <span class="chip" id="chip-phase">阶段: -</span>
          <span class="chip" id="chip-type">类型: -</span>
        </div>
      </section>
    </aside>
    <section class="panel main">
      <section class="scene">
        <div class="room-glow"></div>
        <svg id="action-overlay" class="action-overlay"></svg>
        <div id="action-badges" class="action-badges"></div>
        <div class="bubble">
          <small id="bubble-meta">准备中</small>
          <strong id="bubble-speaker">等待加载日志</strong>
          <p id="bubble-text">加载后会根据日志逐步回放整局游戏。</p>
        </div>
        <div class="table"></div>
        <div id="players-layer"></div>
      </section>
      <section class="footer">
        <div class="panel timeline">
          <div class="timeline-header">
            <h2>回放进度</h2>
            <span class="progress-meta" id="progress-meta">0 / 0</span>
          </div>
          <div class="range-row">
            <input id="event-range" type="range" min="0" max="0" value="0">
            <div class="counter" id="event-counter">0</div>
          </div>
          <div class="chips" id="player-summary"></div>
        </div>
        <div class="panel feed">
          <h2>事件流</h2>
          <div class="feed-list" id="feed-list"></div>
        </div>
      </section>
    </section>
  </main>
  <script>
    const EVENT_LABELS = {
      setup: "入场",
      round: "回合开始",
      phase: "阶段切换",
      speech: "玩家发言",
      attack: "狼人行动",
      inspect: "查验结果",
      save: "女巫救人",
      poison: "女巫毒杀",
      death: "死亡结算",
      vote: "投票结果",
      result: "夜晚结果",
      ending: "终局",
      narration: "旁白"
    };
    const EVENT_EMOJIS = {
      setup: "🎭",
      round: "🎬",
      phase: "🕰️",
      speech: "💬",
      attack: "🐺",
      inspect: "🔮",
      save: "🧪",
      poison: "☠️",
      death: "❌",
      vote: "🗳️",
      result: "📣",
      ending: "🎉",
      narration: "📜"
    };

    const state = { files: [], replay: null, currentIndex: 0, timer: null, typingTimer: null, typingToken: 0, speed: 1.5 };
    const fileSelect = document.getElementById("file-select");
    const loadBtn = document.getElementById("load-btn");
    const playBtn = document.getElementById("play-btn");
    const pauseBtn = document.getElementById("pause-btn");
    const prevBtn = document.getElementById("prev-btn");
    const nextBtn = document.getElementById("next-btn");
    const speedRange = document.getElementById("speed-range");
    const speedText = document.getElementById("speed-text");
    const eventRange = document.getElementById("event-range");
    const eventCounter = document.getElementById("event-counter");
    const progressMeta = document.getElementById("progress-meta");
    const sceneEl = document.querySelector(".scene");
    const tableEl = document.querySelector(".table");
    const bubbleEl = document.querySelector(".bubble");
    const playersLayer = document.getElementById("players-layer");
    const actionOverlay = document.getElementById("action-overlay");
    const actionBadges = document.getElementById("action-badges");
    const feedList = document.getElementById("feed-list");
    const playerSummary = document.getElementById("player-summary");

    function stopPlayback() {
      if (state.timer) { clearInterval(state.timer); state.timer = null; }
    }

    function stopTyping() {
      if (state.typingTimer) {
        clearInterval(state.typingTimer);
        state.typingTimer = null;
      }
    }

    function getPhaseEmoji(phase) {
      if (!phase) return "🕰️";
      if (phase.includes("白天")) return "☀️";
      if (phase.includes("夜")) return "🌙";
      return "🕰️";
    }

    function getEyesEmoji(text) {
      if (!text) return "";
      if (text.includes("睁眼")) return "👀";
      if (text.includes("闭眼")) return "🙈";
      return "";
    }

    function getEventLabel(event) {
      return EVENT_LABELS[event.type] || event.type || "事件";
    }

    function getEventEmoji(event) {
      const eyeEmoji = getEyesEmoji(event.text);
      if (eyeEmoji) return eyeEmoji;
      return EVENT_EMOJIS[event.type] || "✨";
    }

    function decorateEventText(event) {
      const eyeEmoji = getEyesEmoji(event.text);
      if (eyeEmoji) return `${eyeEmoji} ${event.text}`;
      if (event.type === "phase") return `${getPhaseEmoji(event.phase)} ${event.text}`;
      return event.text;
    }

    function applySceneMood(event) {
      sceneEl.classList.remove("scene-day", "scene-night");
      if (event.phase && event.phase.includes("夜")) {
        sceneEl.classList.add("scene-night");
        return;
      }
      if (event.phase && event.phase.includes("白天")) {
        sceneEl.classList.add("scene-day");
        return;
      }
      sceneEl.classList.add("scene-day");
    }

    function getPlayerNode(name) {
      return Array.from(playersLayer.querySelectorAll(".player")).find((node) => node.dataset.name === name) || null;
    }

    function getActorNames(event) {
      if (event.actor) return [event.actor];
      if (!event.actor_role || !event.players) return [];
      const matches = event.players.filter((player) => player.role === event.actor_role && player.alive).map((player) => player.name);
      if (event.actor_role === "狼人") return matches.slice(0, 2);
      return matches.slice(0, 1);
    }

    function getTargetNames(event) {
      if (event.target) return [event.target];
      return event.focus_players || [];
    }

    function isActionEvent(event) {
      return ["attack", "inspect", "save", "poison"].includes(event.type);
    }

    function getAwakePlayers(event) {
      if (!event.players) return [];
      if (event.phase && event.phase.includes("白天")) {
        return event.players.filter((player) => player.alive).map((player) => player.name);
      }

      const text = event.text || "";
      const aliveByRole = (role) => event.players.filter((player) => player.alive && player.role === role).map((player) => player.name);

      if (text.includes("请闭眼")) {
        return [];
      }
      if (text.includes("狼人请睁眼") || event.type === "attack") {
        return aliveByRole("狼人");
      }
      if (text.includes("预言家请睁眼") || event.type === "inspect") {
        return aliveByRole("预言家");
      }
      if (text.includes("女巫请睁眼") || event.type === "save" || event.type === "poison") {
        return aliveByRole("女巫");
      }
      if (event.speaker) {
        return [event.speaker];
      }
      if (event.actor) {
        return [event.actor];
      }
      return [];
    }

    function getExpressionClass(event, player, actorNames, targetNames) {
      if (!player.alive) return "expression-neutral";
      if (player.name === event.speaker) return "expression-speaking";
      if (actorNames.includes(player.name)) return "expression-acting";
      if (targetNames.includes(player.name)) return "expression-target";
      if (event.type === "ending" && player.alive) return "expression-happy";
      if (event.focus_players.includes(player.name)) return "expression-focused";
      return "expression-neutral";
    }

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function getRelativeRect(element, containerRect) {
      const rect = element.getBoundingClientRect();
      return {
        left: rect.left - containerRect.left,
        top: rect.top - containerRect.top,
        right: rect.right - containerRect.left,
        bottom: rect.bottom - containerRect.top,
        width: rect.width,
        height: rect.height,
      };
    }

    function getOverlapArea(a, b) {
      const width = Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
      const height = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));
      return width * height;
    }

    function createPlayerNode(player, index, total) {
      const node = document.createElement("div");
      node.className = "player";
      node.dataset.name = player.name;
      node.style.setProperty("--robe", player.color);
      const angle = ((Math.PI * 2) / total) * index - Math.PI / 2;
      const radiusX = window.innerWidth < 720 ? 36 : 40;
      const radiusY = window.innerWidth < 720 ? 24 : 28;
      node.style.left = `${50 + Math.cos(angle) * radiusX}%`;
      node.style.top = `${58 + Math.sin(angle) * radiusY}%`;
      node.style.animationDelay = `${index * 0.18}s`;
      node.innerHTML = `
        <div class="dead-mark">❌</div>
        <div class="turn-arrow">➜</div>
        <div class="halo"></div>
        <div class="avatar-shell">
          <div class="hair"></div>
          <div class="head">
            <div class="brow left"></div>
            <div class="brow right"></div>
            <div class="eye left"></div>
            <div class="eye right"></div>
            <div class="blush left"></div>
            <div class="blush right"></div>
            <div class="mouth"></div>
          </div>
          <div class="arm left"></div>
          <div class="arm right"></div>
          <div class="body"></div>
          <div class="leg left"></div>
          <div class="leg right"></div>
        </div>
        <div class="name-tag">
          <strong>${player.name}</strong>
          <span>${player.role}</span>
        </div>`;
      return node;
    }

    function renderPlayers(snapshot, event) {
      const total = snapshot.length || 1;
      const actorNames = getActorNames(event);
      const targetNames = getTargetNames(event);
      const awakeNames = getAwakePlayers(event);
      const showActionArrow = isActionEvent(event);
      const existing = new Map();
      playersLayer.querySelectorAll(".player").forEach((node) => existing.set(node.dataset.name, node));
      snapshot.forEach((player, index) => {
        let node = existing.get(player.name);
        if (!node) {
          node = createPlayerNode(player, index, total);
          playersLayer.appendChild(node);
        }
        node.classList.toggle("is-current", event.speaker === player.name);
        node.classList.toggle("is-actor", actorNames.includes(player.name));
        node.classList.toggle("is-acting", showActionArrow && actorNames.includes(player.name));
        node.classList.toggle("is-target", targetNames.includes(player.name));
        node.classList.toggle("is-focused", event.focus_players.includes(player.name));
        node.classList.toggle("is-dead", !player.alive);
        node.classList.toggle("is-awake", player.alive && awakeNames.includes(player.name));
        node.classList.toggle("is-asleep", player.alive && !awakeNames.includes(player.name));
        node.classList.remove("expression-neutral", "expression-speaking", "expression-acting", "expression-target", "expression-happy", "expression-focused");
        node.classList.add(getExpressionClass(event, player, actorNames, targetNames));
        node.style.setProperty("--robe", player.color);
        node.querySelector(".name-tag span").textContent = player.alive ? player.role : `${player.role} · ${player.status_text}`;
      });
    }

    function resetBubblePosition() {
      bubbleEl.classList.remove("is-player");
      bubbleEl.dataset.anchor = "";
      bubbleEl.style.left = "50%";
      bubbleEl.style.top = "18px";
      bubbleEl.style.transform = "translateX(-50%)";
    }

    function positionBubbleNearNode(node) {
      if (!node) {
        resetBubblePosition();
        return;
      }
      const sceneRect = sceneEl.getBoundingClientRect();
      const nodeRect = getRelativeRect(node, sceneRect);
      const tableRect = getRelativeRect(tableEl, sceneRect);
      bubbleEl.classList.add("is-player");
      bubbleEl.style.transform = "none";

      const bubbleWidth = Math.min(320, sceneRect.width - 36);
      const bubbleHeight = bubbleEl.offsetHeight || 120;
      const gap = 24;
      const nodeCenterX = nodeRect.left + nodeRect.width / 2;
      const nodeCenterY = nodeRect.top + nodeRect.height / 2;
      const candidates = [
        {
          anchor: "left",
          left: nodeRect.right + gap,
          top: nodeCenterY - bubbleHeight / 2,
        },
        {
          anchor: "right",
          left: nodeRect.left - bubbleWidth - gap,
          top: nodeCenterY - bubbleHeight / 2,
        },
        {
          anchor: "bottom",
          left: nodeCenterX - bubbleWidth / 2,
          top: nodeRect.top - bubbleHeight - gap,
        },
        {
          anchor: "top",
          left: nodeCenterX - bubbleWidth / 2,
          top: nodeRect.bottom + gap,
        },
        {
          anchor: "left",
          left: nodeRect.right + gap,
          top: nodeRect.top - bubbleHeight * 0.72,
        },
        {
          anchor: "left",
          left: nodeRect.right + gap,
          top: nodeRect.bottom - bubbleHeight * 0.28,
        },
        {
          anchor: "right",
          left: nodeRect.left - bubbleWidth - gap,
          top: nodeRect.top - bubbleHeight * 0.72,
        },
        {
          anchor: "right",
          left: nodeRect.left - bubbleWidth - gap,
          top: nodeRect.bottom - bubbleHeight * 0.28,
        },
      ];

      let bestCandidate = null;
      let bestScore = Number.POSITIVE_INFINITY;

      candidates.forEach((candidate) => {
        const left = clamp(candidate.left, 16, sceneRect.width - bubbleWidth - 16);
        const top = clamp(candidate.top, 16, sceneRect.height - bubbleHeight - 16);
        const rect = {
          left,
          top,
          right: left + bubbleWidth,
          bottom: top + bubbleHeight,
        };
        const adjustPenalty = Math.abs(left - candidate.left) + Math.abs(top - candidate.top);
        const overlapWithTable = getOverlapArea(rect, tableRect);
        const overlapWithSpeaker = getOverlapArea(rect, nodeRect);
        let overlapWithOthers = 0;
        playersLayer.querySelectorAll(".player").forEach((otherNode) => {
          if (otherNode === node) return;
          const otherRect = getRelativeRect(otherNode, sceneRect);
          overlapWithOthers += getOverlapArea(rect, otherRect);
        });
        const score = overlapWithTable * 5 + overlapWithSpeaker * 6 + overlapWithOthers * 7 + adjustPenalty * 24;

        if (score < bestScore) {
          bestScore = score;
          bestCandidate = {
            anchor: candidate.anchor,
            left,
            top,
          };
        }
      });

      if (!bestCandidate) {
        resetBubblePosition();
        return;
      }

      bubbleEl.dataset.anchor = bestCandidate.anchor;
      bubbleEl.style.left = `${bestCandidate.left}px`;
      bubbleEl.style.top = `${bestCandidate.top}px`;
    }

    function startTypewriter(fullText, onProgress = null) {
      const bubbleText = document.getElementById("bubble-text");
      const token = ++state.typingToken;
      stopTyping();

      if (!fullText) {
        bubbleText.textContent = "";
        return;
      }

      bubbleText.textContent = "";
      let index = 0;
      const step = fullText.length > 80 ? 3 : fullText.length > 36 ? 2 : 1;

      state.typingTimer = setInterval(() => {
        if (token !== state.typingToken) {
          stopTyping();
          return;
        }

        index = Math.min(fullText.length, index + step);
        bubbleText.textContent = fullText.slice(0, index);
        if (onProgress) onProgress(index, fullText.length);

        if (index >= fullText.length) {
          stopTyping();
        }
      }, 26);
    }

    function renderBubble(event) {
      const eventLabel = getEventLabel(event);
      const eventEmoji = getEventEmoji(event);
      const phaseLabel = `${getPhaseEmoji(event.phase)} ${event.phase || "未知阶段"}`.trim();
      document.getElementById("bubble-meta").textContent = `${phaseLabel} · ${eventEmoji} ${eventLabel}${event.round ? ` · 第 ${event.round} 轮` : ""}`;
      document.getElementById("bubble-speaker").textContent = `${eventEmoji} ${event.speaker || eventLabel}`;
      document.getElementById("chip-round").textContent = `回合: ${event.round || "-"}`;
      document.getElementById("chip-phase").textContent = `阶段: ${phaseLabel}`;
      document.getElementById("chip-type").textContent = `类型: ${eventEmoji} ${eventLabel}`;

      const decoratedText = decorateEventText(event);
      const actorNames = getActorNames(event);
      if (event.speaker) {
        positionBubbleNearNode(getPlayerNode(event.speaker));
      } else if (actorNames.length === 1) {
        positionBubbleNearNode(getPlayerNode(actorNames[0]));
      } else {
        resetBubblePosition();
      }

      startTypewriter(decoratedText, () => {
        if (event.speaker) {
          positionBubbleNearNode(getPlayerNode(event.speaker));
        } else if (actorNames.length === 1) {
          positionBubbleNearNode(getPlayerNode(actorNames[0]));
        }
      });
    }

    function renderActionOverlay(event) {
      actionOverlay.innerHTML = "";
      actionBadges.innerHTML = "";

      const actorNames = getActorNames(event);
      const targetNames = getTargetNames(event);
      if (!actorNames.length || !targetNames.length) return;

      const sceneRect = sceneEl.getBoundingClientRect();
      const targetNode = getPlayerNode(targetNames[0]);
      if (!targetNode) return;

      const svgNS = "http://www.w3.org/2000/svg";
      actionOverlay.setAttribute("viewBox", `0 0 ${sceneRect.width} ${sceneRect.height}`);

      const defs = document.createElementNS(svgNS, "defs");
      const marker = document.createElementNS(svgNS, "marker");
      marker.setAttribute("id", "action-arrow");
      marker.setAttribute("markerWidth", "12");
      marker.setAttribute("markerHeight", "12");
      marker.setAttribute("refX", "10");
      marker.setAttribute("refY", "6");
      marker.setAttribute("orient", "auto");
      marker.setAttribute("markerUnits", "strokeWidth");
      const arrowPath = document.createElementNS(svgNS, "path");
      arrowPath.setAttribute("d", "M 0 0 L 12 6 L 0 12 z");
      arrowPath.setAttribute("fill", "#8d3122");
      marker.appendChild(arrowPath);
      defs.appendChild(marker);
      actionOverlay.appendChild(defs);

      const targetRect = targetNode.getBoundingClientRect();
      const tx = targetRect.left - sceneRect.left + targetRect.width / 2;
      const ty = targetRect.top - sceneRect.top + targetRect.height / 2 - 22;
      const eventEmoji = getEventEmoji(event);

      actorNames.forEach((name) => {
        const actorNode = getPlayerNode(name);
        if (!actorNode) return;

        const actorRect = actorNode.getBoundingClientRect();
        const ax = actorRect.left - sceneRect.left + actorRect.width / 2;
        const ay = actorRect.top - sceneRect.top + actorRect.height / 2 - 22;

        const dx = tx - ax;
        const dy = ty - ay;
        const distance = Math.hypot(dx, dy) || 1;
        const nx = -dy / distance;
        const ny = dx / distance;
        const curve = Math.min(72, Math.max(28, distance * 0.18));
        const cx = (ax + tx) / 2 + nx * curve;
        const cy = (ay + ty) / 2 + ny * curve;

        const line = document.createElementNS(svgNS, "path");
        line.setAttribute("d", `M ${ax} ${ay} Q ${cx} ${cy} ${tx} ${ty}`);
        line.setAttribute("class", "action-line");
        line.setAttribute("marker-end", "url(#action-arrow)");
        actionOverlay.appendChild(line);

        const badge = document.createElement("div");
        badge.className = "action-badge";
        badge.textContent = eventEmoji;
        badge.style.left = `${cx}px`;
        badge.style.top = `${cy - 10}px`;
        actionBadges.appendChild(badge);
      });
    }

    function renderFeed() {
      if (!state.replay) return;
      const start = Math.max(0, state.currentIndex - 3);
      const end = Math.min(state.replay.events.length, state.currentIndex + 6);
      feedList.innerHTML = "";
      state.replay.events.slice(start, end).forEach((event, offset) => {
        const actualIndex = start + offset;
        const item = document.createElement("div");
        item.className = `feed-item${actualIndex === state.currentIndex ? " active" : ""}`;
        const eventLabel = EVENT_LABELS[event.type] || event.type || "事件";
        const eventEmoji = getEventEmoji(event);
        const phaseLabel = `${getPhaseEmoji(event.phase)} ${event.phase || "未知阶段"}`.trim();
        item.innerHTML = `<small>${phaseLabel}${event.round ? ` · 第 ${event.round} 轮` : ""}</small><strong>${eventEmoji} ${event.speaker || eventLabel}</strong><p>${decorateEventText(event)}</p>`;
        item.addEventListener("click", () => { state.currentIndex = actualIndex; renderCurrent(); });
        feedList.appendChild(item);
      });
    }

    function renderSummary(snapshot) {
      playerSummary.innerHTML = "";
      snapshot.forEach((player) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = `${player.alive ? "🙂" : "❌"} ${player.name} · ${player.alive ? "存活" : "出局"}`;
        chip.style.borderColor = player.alive ? `${player.color}33` : "rgba(110,110,110,0.18)";
        chip.style.background = player.alive ? `${player.color}14` : "rgba(132,117,106,0.12)";
        playerSummary.appendChild(chip);
      });
    }

    function renderCurrent() {
      if (!state.replay || !state.replay.events.length) return;
      const event = state.replay.events[state.currentIndex];
      applySceneMood(event);
      renderPlayers(event.players, event);
      renderBubble(event);
      renderActionOverlay(event);
      renderFeed();
      renderSummary(event.players);
      eventRange.max = String(Math.max(0, state.replay.events.length - 1));
      eventRange.value = String(state.currentIndex);
      eventCounter.textContent = String(state.currentIndex + 1);
      progressMeta.textContent = `${state.currentIndex + 1} / ${state.replay.events.length}`;
    }

    function stepForward() {
      if (!state.replay) return;
      if (state.currentIndex >= state.replay.events.length - 1) { stopPlayback(); return; }
      state.currentIndex += 1;
      renderCurrent();
    }

    function startPlayback() {
      if (!state.replay || state.replay.events.length <= 1) return;
      stopPlayback();
      const interval = Math.max(420, 1600 / state.speed);
      state.timer = setInterval(stepForward, interval);
    }

    async function loadFiles() {
      const response = await fetch("/api/files");
      const payload = await response.json();
      state.files = payload.files || [];
      fileSelect.innerHTML = "";
      state.files.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.id;
        fileSelect.appendChild(option);
      });
    }

    async function loadReplay(fileId = null) {
      const query = fileId ? `?file=${encodeURIComponent(fileId)}` : "";
      const response = await fetch(`/api/replay${query}`);
      const payload = await response.json();
      if (payload.error) {
        document.getElementById("bubble-speaker").textContent = "加载失败";
        document.getElementById("bubble-text").textContent = payload.error;
        return;
      }
      state.replay = payload;
      state.currentIndex = 0;
      stopPlayback();
      document.getElementById("chip-file").textContent = `文件: ${payload.source_file}`;
      renderCurrent();
    }

    loadBtn.addEventListener("click", async () => await loadReplay(fileSelect.value));
    playBtn.addEventListener("click", startPlayback);
    pauseBtn.addEventListener("click", stopPlayback);
    prevBtn.addEventListener("click", () => { stopPlayback(); if (!state.replay) return; state.currentIndex = Math.max(0, state.currentIndex - 1); renderCurrent(); });
    nextBtn.addEventListener("click", () => { stopPlayback(); if (!state.replay) return; state.currentIndex = Math.min(state.replay.events.length - 1, state.currentIndex + 1); renderCurrent(); });
    eventRange.addEventListener("input", () => { stopPlayback(); state.currentIndex = Number(eventRange.value); renderCurrent(); });
    speedRange.addEventListener("input", () => { state.speed = Number(speedRange.value); speedText.textContent = `${state.speed.toFixed(1)}x`; if (state.timer) startPlayback(); });
    window.addEventListener("resize", () => state.replay && renderCurrent());

    (async function init() {
      await loadFiles();
      if (state.files.length) {
        await loadReplay(state.files[0].id);
      } else {
        document.getElementById("bubble-speaker").textContent = "未找到日志";
        document.getElementById("bubble-text").textContent = "项目目录下没有可回放的 .txt 日志文件。";
      }
    })();
  </script>
</body>
</html>
"""


class ReplayHandler(BaseHTTPRequestHandler):
    server_version = "ThreeKingdomsReplay/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return

        if parsed.path == "/api/files":
            self._send_json({"files": discover_log_files()})
            return

        if parsed.path == "/api/replay":
            params = parse_qs(parsed.query)
            file_id = params.get("file", [None])[0]
            try:
                replay = parse_game_log(resolve_log_file(file_id))
            except (FileNotFoundError, ValueError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"source_file": replay.source_file, "players": replay.players, "events": replay.events, "raw_text": replay.raw_text})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="三国狼人杀日志回放 Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=7860, help="监听端口，默认 7860")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ReplayHandler)
    print(f"Replay UI running on http://{server.server_address[0]}:{server.server_address[1]}")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down replay server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
