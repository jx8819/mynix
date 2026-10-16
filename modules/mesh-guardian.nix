# mesh-guardian — 小米 Mesh 有线回程自愈守护进程
#
# 机制完全公开；AP IP、密码、邮箱等私有值由调用方经 options 指定。
{ config, lib, pkgs, ... }:

let
  cfg = config.services.meshGuardian;

  satelliteArgs = lib.concatMapStringsSep " " (s: "--satellite ${s.name}=${s.ip}") cfg.satellites;
in
{
  options.services.meshGuardian = {
    enable = lib.mkEnableOption "Xiaomi Mesh wired-backhaul recovery guardian";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../pkgs/mesh-guardian { };
      description = "The mesh-guardian package.";
    };

    # ── AP 地址 ─────────────────────────────────────────────────────────

    controllerIp = lib.mkOption {
      type = lib.types.str;
      description = "IP address of the controller (master) AP.";
      example = "192.168.1.1";
    };

    satellites = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            description = "Human-readable label for this satellite AP (used in logs and alerts).";
          };
          ip = lib.mkOption {
            type = lib.types.str;
            description = "IP address of this satellite AP.";
          };
        };
      });
      default = [];
      description = "List of satellite APs to monitor and recover.";
    };

    # ── 认证 ────────────────────────────────────────────────────────────────

    passwordFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to the private AP admin password file, supplied by the deployment.";
    };

    # ── 时间参数 ────────────────────────────────────────────────────────────

    checkInterval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 60;
      description = "Seconds between topology polls.";
    };

    wirelessDwell = lib.mkOption {
      type = lib.types.ints.positive;
      default = 120;
      description = "Seconds an AP must remain on wireless backhaul before a reboot is triggered. Prevents reacting to momentary topology flaps.";
    };

    rebootWait = lib.mkOption {
      type = lib.types.ints.positive;
      default = 180;
      description = "Seconds to wait for wired-backhaul recovery after issuing a reboot.";
    };

    cooldown = lib.mkOption {
      type = lib.types.ints.positive;
      default = 300;
      description = "Seconds of mandatory calm after a successful recovery before resuming normal monitoring. Prevents a second reboot during the post-reboot settle window.";
    };

    unreachableThreshold = lib.mkOption {
      type = lib.types.ints.positive;
      default = 2;
      description = ''
        Consecutive failed ping probes required before an AP is declared
        unreachable. Debounce: a single dropped probe (cold ARP right after the
        service starts, transient loss) is ignored instead of logging a false
        outage. With the default checkInterval of 60s, 2 means roughly one
        missed cycle — still short enough to catch a genuine AP reboot.
      '';
    };

    offlineGrace = lib.mkOption {
      type = lib.types.ints.positive;
      default = 600;
      description = ''
        Seconds an AP may be completely unreachable before the guardian considers
        it a long-term absence (power cut, maintenance, switch restart).
        No reboot is issued during this window, and the grace timer
        resets to zero once the AP comes back. If the AP is still unreachable
        after this window it is left alone — no reboot is ever sent to an
        unreachable device.
      '';
    };

    # ── 失败上限与告警 ────────────────────────────────────────────────────

    maxFailures = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = ''
        Number of consecutive failed recovery attempts before the guardian latches
        (stops all automatic action for that AP) and sends an alert email.
        After receiving the email you can reset by restarting the service:
          systemctl restart mesh-guardian
      '';
    };

    mailTo = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Alert recipient email address. Leave empty to disable mail.";
      example = "admin@example.com";
    };

    mailFrom = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Sender address. Must match the postfix relay account so generic-map rewriting works correctly.";
      example = "noreply@example.com";
    };

    sendmail = lib.mkOption {
      type = lib.types.str;
      default = "/run/wrappers/bin/sendmail";
      description = "Path to the sendmail binary used for alert delivery.";
    };

    # ── 进阶 ─────────────────────────────────────────────────────────────

    apiTimeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 10;
      description = "HTTP timeout in seconds for Xiaomi API calls.";
    };

    pingTimeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 2;
      description = "Ping timeout in seconds per AP reachability check.";
    };

    stateFile = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/mesh-guardian/state.json";
      description = "Path to the JSON state persistence file. Survives service restarts; LATCH is deliberately not restored, so restarting the service resumes monitoring.";
    };

    debug = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable verbose debug logging to journald.";
    };
  };

  config = lib.mkIf cfg.enable {

    assertions = [
      {
        assertion = cfg.satellites != [];
        message = "services.meshGuardian.satellites must list at least one AP.";
      }
    ];

    users.users.mesh-guardian = {
      isSystemUser = true;
      group = "mesh-guardian";
      description = "mesh-guardian service account";
    };
    users.groups.mesh-guardian = {};

    systemd.services.mesh-guardian = {
      description = "Xiaomi Mesh wired-backhaul recovery guardian";
      after = [ "network-online.target" "sops-nix.service" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      # ping 在 iputils 里；缺了 is_reachable() 恒失败。
      path = [ pkgs.iputils ];

      serviceConfig = {
        Type = "simple";
        User = "mesh-guardian";
        Group = "mesh-guardian";

        ExecStart = lib.concatStringsSep " " ([
          "${cfg.package}/bin/mesh-guardian"
          "--controller-ip" cfg.controllerIp
          satelliteArgs
          "--password-file" cfg.passwordFile
          "--check-interval"   (toString cfg.checkInterval)
          "--wireless-dwell"   (toString cfg.wirelessDwell)
          "--reboot-wait"      (toString cfg.rebootWait)
          "--cooldown"         (toString cfg.cooldown)
          "--offline-grace"   (toString cfg.offlineGrace)
          "--unreachable-threshold" (toString cfg.unreachableThreshold)
          "--max-failures"     (toString cfg.maxFailures)
          "--api-timeout"      (toString cfg.apiTimeout)
          "--ping-timeout"     (toString cfg.pingTimeout)
          "--state-file"       cfg.stateFile
        ]
        ++ lib.optional (cfg.mailTo != "") "--mail-to ${cfg.mailTo}"
        ++ lib.optional (cfg.mailFrom != "") "--mail-from ${cfg.mailFrom}"
        ++ [ "--sendmail" cfg.sendmail ]
        ++ lib.optional cfg.debug "--debug"
        );

        Restart = "on-failure";
        RestartSec = "30s";

        # 状态目录：状态 JSON 跨重启保留；LATCH 不从状态恢复，重启服务即重新开始。
        StateDirectory = "mesh-guardian";
        StateDirectoryMode = "0700";

        # 安全加固
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectControlGroups = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        # AF_NETLINK / AF_PACKET 供 postfix sendmail 的 getifaddrs() 使用。
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK" "AF_PACKET" ];
        # sendmail 是 setgid postdrop 的 wrapper，NoNewPrivileges 会让 setgid 失效，
        # 且 postdrop 还要写队列目录：故放行队列目录 + 加入 postdrop 组，缺一告警发不出。
        ReadWritePaths = [ (builtins.dirOf cfg.stateFile) "/var/lib/postfix/queue" ];
        SupplementaryGroups = [ "postdrop" ];
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;

        AmbientCapabilities = [];

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "mesh-guardian";
      };
    };
  };
}
