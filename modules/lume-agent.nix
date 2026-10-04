# Lume 探针监控 Agent NixOS 模块 (MostlyCodex/lume-monitor)
# 出站上报探针：纯出站 HTTPS，无监听端口，通过非特权 ping socket 探测
{ config, lib, pkgs, ... }:

let
  cfg = config.services.lume-agent;
in
{
  options.services.lume-agent = {
    enable = lib.mkEnableOption "Lume VPS 探针监控客户端 (vpsmon-agent)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.lume-agent;
      description = "lume-agent 包。";
    };

    nodeId = lib.mkOption {
      type = lib.types.str;
      description = "节点全局唯一 ID (如 jp, us, kr, nix-lz)。";
    };

    displayName = lib.mkOption {
      type = lib.types.str;
      description = "面板展示名称。";
    };

    role = lib.mkOption {
      type = lib.types.str;
      default = "VPS";
      description = "节点角色。";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "vps";
      description = "分组名称。";
    };

    region = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "地理区域/机房位置。";
    };

    displayOrder = lib.mkOption {
      type = lib.types.int;
      default = 10;
      description = "面板排序。";
    };

    color = lib.mkOption {
      type = lib.types.str;
      default = "green";
      description = "面板卡片配色。";
    };

    endpoint = lib.mkOption {
      type = lib.types.str;
      default = "https://monitor.xjn819.com/api/v1/report";
      description = "Lume 后端上报 API 地址。";
    };

    secretFile = lib.mkOption {
      type = lib.types.path;
      description = "节点 HMAC 签名密钥文件路径 (sops 渲染的 secret)。";
    };

    reportInterval = lib.mkOption {
      type = lib.types.int;
      default = 60;
      description = "数据上报间隔 (秒)。";
    };

    probeInterval = lib.mkOption {
      type = lib.types.int;
      default = 60;
      description = "网络探针执行间隔 (秒)。";
    };

    probes = lib.mkOption {
      type = lib.types.listOf lib.types.attrs;
      default = [ ];
      description = "网络探针列表 (ICMP 或 TCP 探测目标)。";
    };

    monitoredServices = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "sshd.service" ];
      description = "只读监测的 systemd 服务单元名称。";
    };
  };

  config = lib.mkIf cfg.enable {
    # 允许普通用户组使用非特权 datagram ping socket
    boot.kernel.sysctl = {
      "net.ipv4.ping_group_range" = "0 2147483647";
    };

    users.users.vpsmon = {
      isSystemUser = true;
      group = "vpsmon";
      description = "Lume monitoring agent user";
    };

    users.groups.vpsmon = { };

    systemd.services.lume-agent = {
      description = "Lume Monitoring Agent (vpsmon-agent)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        Type = "simple";
        User = "vpsmon";
        Group = "vpsmon";
        Restart = "always";
        RestartSec = "10s";

        # 运行时目录：/run/vpsmon
        RuntimeDirectory = "vpsmon";
        RuntimeDirectoryMode = "0750";

        # 安全沙箱 (符合上游设计契约)
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;

        ExecStartPre = pkgs.writeShellScript "lume-agent-pre-start" ''
          set -euo pipefail
          SECRET=$(cat "${cfg.secretFile}" | tr -d '\r\n')
          ${pkgs.jq}/bin/jq -n \
            --arg id "${cfg.nodeId}" \
            --arg name "${cfg.displayName}" \
            --arg role "${cfg.role}" \
            --arg grp "${cfg.group}" \
            --arg reg "${cfg.region}" \
            --arg color "${cfg.color}" \
            --argjson order ${toString cfg.displayOrder} \
            --arg ep "${cfg.endpoint}" \
            --arg secret "$SECRET" \
            --argjson repInt ${toString cfg.reportInterval} \
            --argjson prbInt ${toString cfg.probeInterval} \
            --argjson srvs '${builtins.toJSON cfg.monitoredServices}' \
            --argjson prbs '${builtins.toJSON cfg.probes}' \
            '{
              node: {
                id: $id,
                display_name: $name,
                role: $role,
                group: $grp,
                region: $reg,
                stale_seconds: 180,
                display_order: $order,
                color: $color,
                offline_severity: "P1",
                ip_change_severity: "P2"
              },
              endpoint: $ep,
              secret: $secret,
              report_interval_seconds: $repInt,
              probe_interval_seconds: $prbInt,
              services: $srvs,
              probes: $prbs,
              spool_path: "/run/vpsmon/spool.json"
            }' > /run/vpsmon/config.json
          chmod 600 /run/vpsmon/config.json
        '';

        ExecStart = "${cfg.package}/bin/lume-agent --config /run/vpsmon/config.json";
      };
    };
  };
}
