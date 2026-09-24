# mesh-guardian — Xiaomi Mesh wired-backhaul recovery guardian
#
# Mechanism is fully public; all private values (AP IPs, passwords, email
# addresses) live exclusively in the caller's private configuration via options.
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

    # ── AP addresses ─────────────────────────────────────────────────────────

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

    # ── Authentication ────────────────────────────────────────────────────────

    passwordSecret = lib.mkOption {
      type = lib.types.str;
      default = "secrets/mesh_guardian_password";
      description = "sops secret key that holds the AP admin password.";
    };

    sopsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "sops YAML file that contains passwordSecret. Required; no default because the file is in the caller's private config.";
    };

    # ── Timing ────────────────────────────────────────────────────────────────

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

    offlineGrace = lib.mkOption {
      type = lib.types.ints.positive;
      default = 600;
      description = ''
        Seconds an AP may be completely unreachable before the guardian considers
        it a long-term absence (power cut, maintenance, switch restart after
        cleaning). No reboot is issued during this window, and the grace timer
        resets to zero once the AP comes back. If the AP is still unreachable
        after this window it is left alone — no reboot is ever sent to an
        unreachable device.
      '';
    };

    # ── Failure cap & alerting ────────────────────────────────────────────────

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

    # ── Advanced ─────────────────────────────────────────────────────────────

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
      description = "Path to the JSON state persistence file. Survives service restarts; deleted by systemd on a clean start only if you use StateDirectory.";
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
        assertion = cfg.sopsFile != null;
        message = "services.meshGuardian.sopsFile must be set to the sops YAML containing the AP password.";
      }
      {
        assertion = cfg.satellites != [];
        message = "services.meshGuardian.satellites must list at least one AP.";
      }
    ];

    sops.secrets.${cfg.passwordSecret} = {
      sopsFile = cfg.sopsFile;
      owner = "mesh-guardian";
      mode = "0400";
    };

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

      # ping lives in iputils; without it is_reachable() always fails.
      path = [ pkgs.iputils ];

      serviceConfig = {
        Type = "simple";
        User = "mesh-guardian";
        Group = "mesh-guardian";

        ExecStart = lib.concatStringsSep " " ([
          "${cfg.package}/bin/mesh-guardian"
          "--controller-ip" cfg.controllerIp
          satelliteArgs
          "--password-file" "/run/secrets/${cfg.passwordSecret}"
          "--check-interval"   (toString cfg.checkInterval)
          "--wireless-dwell"   (toString cfg.wirelessDwell)
          "--reboot-wait"      (toString cfg.rebootWait)
          "--cooldown"         (toString cfg.cooldown)
          "--offline-grace"    (toString cfg.offlineGrace)
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

        # State directory (survives restarts; cleared by manual --unlatch or
        # service restart which reloads persisted latch from the JSON file)
        StateDirectory = "mesh-guardian";
        StateDirectoryMode = "0700";

        # Hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectControlGroups = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;

        ReadWritePaths = [ (builtins.dirOf cfg.stateFile) ];

        # Allow sendmail to be called (it's a setuid wrapper)
        AmbientCapabilities = [];

        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "mesh-guardian";
      };
    };
  };
}
