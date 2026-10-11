# hdsky-checkin — HDSky（hdsky.me）自动签到
#
# Mechanism is fully public; all private values (the login Cookie and the alert
# recipient) live exclusively in the caller's private configuration and are
# sops-encrypted there.
{ config, lib, pkgs, ... }:

let
  cfg = config.services.hdskyCheckin;
in
{
  options.services.hdskyCheckin = {
    enable = lib.mkEnableOption "HDSky (hdsky.me) automatic check-in";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../pkgs/hdsky-checkin { };
      description = "The hdsky-checkin package.";
    };

    # ── Authentication ────────────────────────────────────────────────────────

    cookieFile = lib.mkOption {
      type = lib.types.str;
      description = "Path to the private HDSky Cookie file, supplied by the deployment.";
    };

    userAgent = lib.mkOption {
      type = lib.types.str;
      # 取 cookie 的浏览器是 Safari，UA 保持一致，避免站点侧会话指纹不匹配。
      default = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";
      description = "User-Agent sent with every request. Keep it consistent with the browser the Cookie was taken from.";
    };

    # ── Failure alerting ──────────────────────────────────────────────────────

    mailToFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Path to the private failure-alert recipient file; null disables failure alerts.";
    };

    mailFrom = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Alert sender address. Must match the postfix relay account so generic-map rewriting works correctly. Leave empty to disable alerts.";
      example = "noreply@example.com";
    };

    sendmail = lib.mkOption {
      type = lib.types.str;
      default = "/run/wrappers/bin/sendmail";
      description = "Path to the sendmail binary used for alert delivery.";
    };

    # ── Schedule ──────────────────────────────────────────────────────────────

    onCalendar = lib.mkOption {
      type = lib.types.str;
      default = "Mon *-*-* 04:00:00";
      description = "systemd OnCalendar expression for the check-in run.";
      example = "*-*-* 04:00:00";
    };

    randomizedDelaySec = lib.mkOption {
      type = lib.types.str;
      default = "5m";
      description = "Random delay added to each run, so the hit is not at a perfectly predictable instant.";
    };

    # ── Behaviour ─────────────────────────────────────────────────────────────

    maxAttempts = lib.mkOption {
      type = lib.types.ints.positive;
      default = 8;
      description = "Captcha retry cap. Recognition failures re-fetch a fresh image until this is reached.";
    };

    timeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 20;
      description = "HTTP timeout in seconds for each request.";
    };
  };

  config = lib.mkIf cfg.enable {

    assertions = [
      {
        assertion = cfg.mailToFile == null || cfg.mailFrom != "";
        message = "services.hdskyCheckin.mailFrom must be set when mailToFile is used (postfix needs a sender for generic-map rewriting).";
      }
    ];


    users.users.hdsky-checkin = {
      isSystemUser = true;
      group = "hdsky-checkin";
      description = "hdsky-checkin service account";
    };
    users.groups.hdsky-checkin = {};

    systemd.services.hdsky-checkin = {
      description = "HDSky automatic check-in";
      after = [ "network-online.target" "sops-nix.service" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        Type = "oneshot";
        User = "hdsky-checkin";
        Group = "hdsky-checkin";

        ExecStart = lib.concatStringsSep " " (
          [
            "${cfg.package}/bin/hdsky-checkin"
            "--cookie-file" cfg.cookieFile
            "--user-agent" (lib.escapeShellArg cfg.userAgent)
            "--max-attempts" (toString cfg.maxAttempts)
            "--timeout" (toString cfg.timeout)
          ]
          ++ lib.optional (cfg.mailToFile != null)
            "--mail-to-file ${cfg.mailToFile}"
          ++ lib.optional (cfg.mailFrom != "") "--mail-from ${cfg.mailFrom}"
          ++ [ "--sendmail" cfg.sendmail ]
        );

        # Hardening（与 mesh-guardian 同一套）
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectControlGroups = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        # AF_UNIX 是 sendmail 必需；AF_NETLINK / AF_PACKET 是 postfix 的
        # getifaddrs() 必需（缺了 `Address family not supported by protocol`）。
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" "AF_NETLINK" "AF_PACKET" ];
        # sendmail 是 setgid postdrop 的 wrapper，NoNewPrivileges 会让 setgid 失效；
        # postdrop 还要往队列目录写信。缺任一条告警都发不出去：
        #  - ProtectSystem=strict 让 /var/lib/postfix/queue 只读 →
        #    `mail_queue_enter: ... Read-only file system`
        #  - 没有 postdrop 组 → 写不进 `drwx-wx--- xjn postdrop` 的 maildrop/
        ReadWritePaths = [ "/var/lib/postfix/queue" ];
        SupplementaryGroups = [ "postdrop" ];
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "hdsky-checkin";
      };
    };

    systemd.timers.hdsky-checkin = {
      description = "HDSky automatic check-in timer";
      wantedBy = [ "timers.target" ];

      timerConfig = {
        OnCalendar = cfg.onCalendar;
        # 机器在计划时刻没开机时，开机后补跑一次。
        Persistent = true;
        RandomizedDelaySec = cfg.randomizedDelaySec;
        Unit = "hdsky-checkin.service";
      };
    };
  };
}
