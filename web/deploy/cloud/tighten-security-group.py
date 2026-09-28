#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯云安全组收口（ECAS H-01 配套）：把实例 ins-1bgcyzm3 的指定端口的入站来源
从 0.0.0.0/0 收窄到白名单网段（默认 8883 ← 180.108.106.0/24，与主机侧 nftables 白名单同口径）。

为什么需要它：主机侧 ecas-relay-firewall（nftables）只能挡到「包到达云主机」之后，
安全组是第一道门；两层同口径才叫双保险。

认证：TC3-HMAC-SHA256（标准库实现，无需装 SDK），密钥只从环境变量读，绝不出现在命令行参数里。
    export TENCENTCLOUD_SECRET_ID=...
    export TENCENTCLOUD_SECRET_KEY=...
建议用**只有 VPC 读写权限的 CAM 子用户密钥**，用完即删。

用法：
    ./tighten-security-group.py show                     # 只读：打印实例安全组与全部入站规则
    ./tighten-security-group.py plan                     # 只读：打印「将要做的改动」（不写）
    ./tighten-security-group.py apply --confirm          # 真改（先自动备份原规则到同目录 JSON）
    ./tighten-security-group.py apply                    # 不带 --confirm = 等于 plan，零写入

删除冗余放行（2026-09-28 复核结论，见报告附录 B / 手册 §8.11）：
    ./tighten-security-group.py drop --drop 12121 --drop 11012 --drop 21212/TCP --drop 11011/TCP
    ./tighten-security-group.py trim --confirm            # trim = 上面那组冗余的固定清单，一条命令搞定
    （drop/trim 只删「Action=ACCEPT 且端口/协议命中」的行；拒绝类规则一律保留不动，更不动别人的端口。）
    （`12121` = EasyTier 控制面改走 SSH 隧道；`11012` 全无监听；`21212/TCP` 与 `11011/TCP` 无监听，
      但 `21212/UDP`（设备取配置）与 `11011/UDP`（easytier-core 在用）必须保留。）

参数（默认值即 ECAS 现状）：
    --instance ins-1bgcyzm3 --region ap-shanghai
    --port 8883 --allow 180.108.106.0/24 [--allow 127.0.0.1 ...]

安全设计：
  * 只改「Action=ACCEPT 且端口命中 --port 且来源不是白名单」的入站规则；其余规则原样保留。
  * 提交前完整打印新旧对照；提交后重新 Describe 校验（并打印）。
  * 备份文件 tighten-sg-backup-<时间戳>.json 含改动前全部规则，回滚 = apply 时指向该文件（见 rollback）。
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_INSTANCE = "ins-1bgcyzm3"
DEFAULT_REGION = "ap-shanghai"
DEFAULT_PORT = "8883"
DEFAULT_ALLOW = "180.108.106.0/24"
# 2026-09-28 复核后确认的冗余放行（报告附录 B / 手册 §8.11）：
#   12121        EasyTier 控制面 ⇒ 改走 ssh -L 隧道
#   11012        TCP/UDP 全无监听
#   21212/TCP    无监听（21212/UDP 是设备取配置口，必须留）
#   11011/TCP    无监听（11011/UDP 被 easytier-core 占用，必须留）
DEFAULT_DROPS = ["12121", "11012", "21212/TCP", "11011/TCP"]


# --------------------------------------------------------------------------- #
# 签名与调用
# --------------------------------------------------------------------------- #
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def call(service: str, action: str, payload: dict, region: str,
         secret_id: str, secret_key: str, version: str) -> dict:
    """调一次腾讯云 API（TC3-HMAC-SHA256）。"""
    host = f"{service}.tencentcloudapi.com"
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    ctype = "application/json; charset=utf-8"

    canonical_request = "\n".join([
        "POST", "/", "",
        f"content-type:{ctype}\nhost:{host}\n",
        "content-type;host",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    ])
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(ts), scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _sign(f"TC3{secret_key}".encode("utf-8"), date)
    k_svc = _sign(k_date, service)
    k_sign = _sign(k_svc, "tc3_request")
    signature = hmac.new(k_sign, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders=content-type;host, Signature={signature}")

    req = urllib.request.Request(
        f"https://{host}/",
        data=body.encode("utf-8"),
        headers={
            "Authorization": auth,
            "Content-Type": ctype,
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(ts),
            "X-TC-Version": version,
            "X-TC-Region": region,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:                     # 4xx/5xx 也带 JSON 错误体
        data = json.loads(exc.read().decode("utf-8") or "{}")
    except urllib.error.URLError as exc:
        die(f"网络不可达：{exc.reason}")

    err = data.get("Response", {}).get("Error")
    if err:
        die(f"{action} 失败：{err.get('Code')} — {err.get('Message')}\n"
            f"提示：Code 以 AuthFailure/UnauthorizedOperation 开头 ⇒ 密钥或权限问题；"
            f"密钥必须来自有 VPC 读写权限的 CAM 用户。")
    return data["Response"]


def die(msg: str):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def need_keys() -> tuple:
    sid = os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    if not sid or not skey:
        die("缺少凭据：请 export TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY\n"
            "  （建议用只有 VPC 读写权限的 CAM 子用户密钥；不要用主账号密钥）\n"
            "  另：本脚本也可只读运行，但 show/plan 同样需要密钥（Describe 也要鉴权）。")
    return sid, skey


# --------------------------------------------------------------------------- #
# 规则读写
# --------------------------------------------------------------------------- #
def instance_security_groups(inst: str, region: str, sid: str, skey: str) -> list:
    resp = call("cvm", "DescribeInstances", {"InstanceIds": [inst]}, region, sid, skey, "2017-03-12")
    sets = resp.get("InstanceSet") or []
    if not sets:
        die(f"实例不存在或不可见：{inst}")
    sgs = sets[0].get("SecurityGroupIds") or []
    if not sgs:
        die(f"实例 {inst} 没有绑定安全组（异常）")
    print(f"实例 {inst}（{region}）：{sets[0].get('InstanceName') or '-'} / 状态 {sets[0].get('InstanceState')}")
    print(f"绑定安全组：{', '.join(sgs)}")
    return sgs


def get_policies(sg: str, region: str, sid: str, skey: str) -> dict:
    resp = call("vpc", "DescribeSecurityGroupPolicies", {"SecurityGroupId": sg},
                region, sid, skey, "2017-03-12")
    return (resp.get("SecurityGroupPolicySet") or {})


def port_hit(rule_port: str, target: str) -> bool:
    """安全组端口写法：'8883' / '8883,8884' / '8883-8890' / 'ALL'。"""
    if not rule_port:
        return False
    if rule_port.upper() in ("ALL", "*"):
        return True
    for part in str(rule_port).replace(" ", "").split(","):
        if part == target:
            return True
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit() and int(lo) <= int(target) <= int(hi):
                return True
    return False


def is_broad(cidr: str) -> bool:
    return cidr in ("0.0.0.0/0", "::/0")


def build_new_ingress(rules: list, port: str, allow: list) -> tuple:
    """返回 (新规则列表, 改动清单)。只动「ACCEPT + 命中端口 + 来源非白名单」的规则。"""
    new, changes = [], []
    seen_allow = False
    for r in rules:
        if r.get("Action") != "ACCEPT" or not port_hit(r.get("Port", ""), port):
            new.append(dict(r))
            continue
        old = r.get("CidrBlock", "")
        if is_broad(old):
            fixed = dict(r)
            fixed["CidrBlock"] = allow[0]                      # 第一条白名单
            fixed["Action"] = "ACCEPT"
            new.append(fixed)
            changes.append(("收窄", old, allow[0], r.get("Port")))
        elif old in allow:
            new.append(dict(r))
            seen_allow = True
            changes.append(("已符合", old, old, r.get("Port")))
        else:
            # 其它具体来源：保留不动（不擅自删除他人规则），但提示
            new.append(dict(r))
            changes.append(("保留", old, old, r.get("Port")))
    if not seen_allow and not any(c[0] == "收窄" for c in changes):
        # 原本没有该端口的 ACCEPT 规则 ⇒ 补一条白名单（不动其它端口）
        new.append({"Action": "ACCEPT", "CidrBlock": allow[0], "Protocol": "tcp",
                    "Port": port, "Description": "ECAS relay (tightened)"})
        changes.append(("新增", "(无)", allow[0], port))
    return new, changes


def spec_hit(rule: dict, spec: str) -> bool:
    """判断一条规则是否命中删除规格（形如 '12121' 或 '21212/TCP'）。
    只命中 ACCEPT 行；带协议后缀时，Protocol=ALL 的行视为**不命中**（宁可不删，避免误删大范围规则）。"""
    port, _, proto = spec.partition("/")
    if rule.get("Action") != "ACCEPT":
        return False
    if not port_hit(rule.get("Port", ""), port):
        return False
    if not proto:
        return True
    rp = str(rule.get("Protocol", "")).upper()
    if rp in ("ALL", "*"):
        return False
    return proto.upper() == rp


def build_drops(rules: list, specs: list) -> tuple:
    """返回 (新规则列表, 处置清单)。只删命中规格的放行行，其余（含全部拒绝行）原样保留。"""
    new, changes = [], []
    for r in rules:
        hit = next((s for s in specs if spec_hit(r, s)), None)
        if hit:
            changes.append(("删除", hit, f"{r.get('Protocol')}/{r.get('Port')}",
                            r.get("CidrBlock") or r.get("CidrBlockV6") or "-",
                            r.get("Description") or ""))
            continue
        new.append(dict(r))
    return new, changes


def apply_ingress(sg: str, ingress: list, region: str, sid: str, skey: str) -> dict:
    return call("vpc", "ModifySecurityGroupPolicies",
                {"SecurityGroupId": sg, "SecurityGroupPolicySet": {"Ingress": ingress}},
                region, sid, skey, "2017-03-12")


def show_rules(tag: str, ingress: list):
    print(f"\n--- {tag} ---")
    if not ingress:
        print("  (空)")
    for r in ingress:
        print(f"  {'ACCEPT' if r.get('Action') == 'ACCEPT' else r.get('Action','?'):6} "
              f"{str(r.get('Protocol','-')):5} {str(r.get('Port','-')):12} "
              f"{r.get('CidrBlock') or r.get('CidrBlockV6') or '-':20} {r.get('Description') or ''}")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="腾讯云安全组收口（ECAS relay 8883 / 冗余放行清理）")
    ap.add_argument("cmd", choices=["show", "plan", "apply", "drop", "trim"],
                    help="show/plan/apply = 收窄 8883 来源；drop/trim = 删除冗余放行规则")
    ap.add_argument("--instance", default=DEFAULT_INSTANCE)
    ap.add_argument("--sg-id", default=None, dest="sg_id",
                    help="直接指定安全组 id（如 sg-xxxxxxxx）⇒ 跳过 CVM DescribeInstances，"
                         "此时密钥只需 VPC 读写权限，不必给 CVM 读权限")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--allow", action="append", default=None,
                    help=f"白名单网段，可重复；默认 {DEFAULT_ALLOW}")
    ap.add_argument("--drop", action="append", default=None, dest="drops",
                    help="要删除的放行规则，形如 12121 / 11012 / 21212/TCP；可重复")
    ap.add_argument("--confirm", action="store_true", help="apply/drop/trim 时真写；缺省只打印")
    args = ap.parse_args()

    allow = args.allow or [DEFAULT_ALLOW]
    if args.cmd == "trim":
        specs = args.drops or DEFAULT_DROPS
    elif args.cmd == "drop":
        if not args.drops:
            die("drop 需要 --drop <PORT[/PROTO]>（可重复）；只想删该组冗余可直接用 trim")
        specs = args.drops
    else:
        specs = []
    deleting = args.cmd in ("drop", "trim")

    sid, skey = need_keys()
    if args.sg_id:
        print(f"直接指定安全组：{args.sg_id}（跳过 CVM DescribeInstances）")
        sgs = [args.sg_id]
    else:
        sgs = instance_security_groups(args.instance, args.region, sid, skey)

    for sg in sgs:
        rules = get_policies(sg, args.region, sid, skey)
        ingress = rules.get("Ingress") or []
        show_rules(f"{sg} 当前入站（{len(ingress)} 条）", ingress)

        if deleting:
            print(f"\n拟删除的放行规则（规格：{', '.join(specs)}）：")
            new_ingress, changes = build_drops(ingress, specs)
            if not changes:
                print("  (无匹配行——可能已经删过了)")
            for kind, spec, proto_port, cidr, desc in changes:
                print(f"  [{kind}] 规格 {spec} ← {proto_port} {cidr} {desc}")
            missing = [s for s in specs
                       if not any(spec == s for _, spec, _, _, _ in changes)]
            if missing:
                print(f"  ⚠️ 未命中（请核对 CSV/实际情况）：{', '.join(missing)}")
            show_rules(f"{sg} 拟改为（{len(new_ingress)} 条，删 {len(changes)} 条）", new_ingress)
        else:
            new_ingress, changes = build_new_ingress(ingress, args.port, allow)
            print(f"\n端口 {args.port} 的处置：")
            for kind, old, new, port in changes:
                print(f"  [{kind}] {port}: {old} → {new}")
            if not changes:
                print("  (无匹配规则)")
            show_rules(f"{sg} 拟改为（{len(new_ingress)} 条）", new_ingress)

        if args.cmd == "show":
            print("\n（show：未做任何写入）")
            continue
        if args.cmd == "plan" or not args.confirm:
            print("\n（只读模式：未做任何写入。真改请加 --confirm）")
            continue

        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           f"tighten-sg-backup-{stamp}.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({sg: rules}, fh, ensure_ascii=False, indent=2)
        print(f"\n已备份改动前规则 → {out}")

        apply_ingress(sg, new_ingress, args.region, sid, skey)
        after = (get_policies(sg, args.region, sid, skey).get("Ingress") or [])
        show_rules(f"{sg} 改动后（校验）", after)

        if deleting:
            left = [f"{r.get('Protocol')}/{r.get('Port')} {r.get('CidrBlock')}"
                    for r in after if any(spec_hit(r, s) for s in specs)]
            ok = not left
            print(f"\n{'✓ 删除校验通过' if ok else '✗ 校验失败：仍有命中规格的放行行 ' + '; '.join(left)}："
                  f"{len(after)} 条入站（原 {len(ingress)} 条）")
        else:
            ok = all(not is_broad(r.get("CidrBlock", ""))
                     for r in after
                     if r.get("Action") == "ACCEPT" and port_hit(r.get("Port", ""), args.port))
            print(f"\n{'✓ 收口校验通过' if ok else '✗ 校验失败：仍存在 0.0.0.0/0 来源'}："
                  f"端口 {args.port} 的所有 ACCEPT 规则来源均为具体网段")
        if not ok:
            sys.exit(2)


if __name__ == "__main__":
    main()
