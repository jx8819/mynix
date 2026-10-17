# udpxy — UDP-to-HTTP multicast traffic relay daemon
{ config, lib, pkgs, ... }:

let
  cfg = config.services.udpxy;
in
{
  options.services.udpxy = {
    enable = lib.mkEnableOption "UDP-to-HTTP multicast traffic relay daemon (udpxy)";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.udpxy;
      description = "The udpxy package to use.";
    };

    bindAddress = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Address to listen for HTTP requests on.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 4022;
      description = "Port to listen for HTTP requests on.";
    };

    mcastInterface = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "IPv4 address or interface name of multicast source.";
      example = "10.0.0.4";
    };

    maxClients = lib.mkOption {
      type = lib.types.ints.positive;
      default = 10;
      description = "Maximum number of simultaneous clients.";
    };

    bufferSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 65536;
      description = "Buffer size (in bytes) for inbound multicast data.";
    };

    renewSubscriptionSec = lib.mkOption {
      type = lib.types.int;
      default = 30;
      description = "Periodically renew multicast subscription (0 to disable).";
    };

    enableStats = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Enable HTTP client statistics at /status.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.udpxy = {
      description = "udpxy - Multicast to HTTP unicast proxy";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "simple";
        ExecStart = lib.concatStringsSep " " (
          [
            "${cfg.package}/bin/udpxy"
            "-T"
            "-a ${cfg.bindAddress}"
            "-p ${toString cfg.port}"
            "-c ${toString cfg.maxClients}"
            "-B ${toString cfg.bufferSize}"
            "-M ${toString cfg.renewSubscriptionSec}"
          ]
          ++ lib.optional (cfg.mcastInterface != null) "-m ${cfg.mcastInterface}"
          ++ lib.optional cfg.enableStats "-S"
        );
        Restart = "always";
        RestartSec = 3;
        ProtectSystem = "strict";
        ProtectHome = "true";
        PrivateTmp = "true";
        NoNewPrivileges = "true";
        ProtectControlGroups = "true";
        ProtectKernelModules = "true";
        ProtectKernelTunables = "true";
        LockPersonality = "true";
        MemoryDenyWriteExecute = "true";
        RestrictRealtime = "true";
        RestrictSUIDSGID = "true";
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
      };
    };
  };
}
