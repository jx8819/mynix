# ompweb — Web UI for the OMP coding agent
# ompweb upstream project (link removed: https links are private per policy 2026-09-23)
#
# NixOS module：机制全部公开；主机相关的值都是 options（在调用方的私有配置里赋值）。
{ config, lib, pkgs, ... }:

let
  cfg = config.services.ompweb;

  ompwebPkg = pkgs.callPackage ../pkgs/ompweb { nodeModulesDir = cfg.nodeModulesDir; };
in
{
  options.services.ompweb = {
    enable = lib.mkEnableOption "ompweb Web UI";

    hostname = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "监听地址。只应在本机反代（caddy/nginx 带 TLS+认证）后暴露；0.0.0.0 会明文暴露密码。";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 30177;
      description = "Port to listen on";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "xjn";
      description = "User to run ompweb as";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/mnt/Media/Settings/agent/omp/webui";
      description = "Data persistence directory";
    };

    agentDir = lib.mkOption {
      type = lib.types.path;
      default = "/mnt/Media/Settings/agent/omp";
      description = "OMP agent directory (sessions, memories, config)";
    };

    nodeModulesDir = lib.mkOption {
      type = lib.types.str;
      default = "/opt/ompweb/lib/node_modules";
      description = "@kahme247/ompweb 完整 node_modules 所在目录（需预先 npm install 出依赖树）";
    };

    extraReadWritePaths = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "主机特定的额外可写路径（实例参数，放 services/*/ 开关文件，勿写进本模块）";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "是否在防火墙开放 port。仅当确需绕过反代直连时才开；对外入口应统一走反代（TLS + 密码）。";
    };

    passwordSecret = lib.mkOption {
      type = lib.types.str;
      default = "secrets/ompweb_password";
      description = "sops secret key for the web UI password (as written in the YAML file)";
    };

    sopsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "sops file containing the password（必填，无默认值——密码文件属于调用方私有配置）";
    };
  };

  config = lib.mkIf cfg.enable {
    # 用户通常已在别处定义为 normal user，这里不重复创建；只需要确保组存在。
    users.groups.${cfg.user} = lib.mkIf (!builtins.hasAttr cfg.user config.users.users) { };

    sops.secrets."${cfg.passwordSecret}" = lib.mkIf (cfg.sopsFile != null) {
      sopsFile = cfg.sopsFile;
      owner = cfg.user;
      group = cfg.user;
      mode = "0400";
    };

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 ${cfg.user} ${cfg.user} -"
      "d ${cfg.agentDir} 0750 ${cfg.user} ${cfg.user} -"
      "z ${cfg.agentDir}/config.yml 0640 ${cfg.user} ${cfg.user} - -"
      "z ${cfg.agentDir}/models.yml 0640 ${cfg.user} ${cfg.user} - -"
      "z ${cfg.agentDir}/agent.db 0640 ${cfg.user} ${cfg.user} - -"
      "z ${cfg.agentDir}/models.db 0640 ${cfg.user} ${cfg.user} - -"
    ];

    systemd.services.ompweb = {
      description = "OMP Web UI";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" "sops-install-secrets.service" ];
      wants = [ "network-online.target" ];
      environment = {
        OMP_WEB_HOSTNAME = cfg.hostname;
        OMP_WEB_PORT = toString cfg.port;
        OMP_WEB_NO_OPEN = "1";
        PI_CODING_AGENT_DIR = cfg.agentDir;
        HOME = cfg.dataDir;
        PATH = lib.mkForce (lib.concatStringsSep ":" [ "${pkgs.nodejs}/bin" "/run/current-system/sw/bin" config.security.wrapperDir ]);
      };
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        Restart = "always";
        RestartSec = 5;
        EnvironmentFile = config.sops.templates.ompweb-env.path;
        ExecStart = "${ompwebPkg}/bin/ompweb --hostname ${cfg.hostname} --port ${toString cfg.port}";
        # Plain chown/chmod directly avoids systemd-tmpfiles "unsafe path transition"
        # across /mnt/Media/Settings (xjn) -> agent (agent).
        # Runs as root before privilege drop.
        ExecStartPre = [
          "+${pkgs.coreutils}/bin/chown ${cfg.user}:${cfg.user} ${cfg.agentDir}/config.yml ${cfg.agentDir}/models.yml ${cfg.agentDir}/agent.db ${cfg.agentDir}/models.db"
          "+${pkgs.coreutils}/bin/chmod 0640 ${cfg.agentDir}/config.yml ${cfg.agentDir}/models.yml ${cfg.agentDir}/agent.db ${cfg.agentDir}/models.db"
        ];
        WorkingDirectory = cfg.dataDir;
        # 本服务定位是「管理入口」，防护边界应在网络与认证层（反代密码/TLS），
        # 本单元沙箱仅作防误操作；如需收紧请按主机威胁模型自行加固。
        NoNewPrivileges = false;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        ReadWritePaths = [ cfg.dataDir cfg.agentDir ] ++ cfg.extraReadWritePaths;
        SupplementaryGroups = [ "keys" ];
      };
    };

    sops.templates.ompweb-env = {
      owner = cfg.user;
      group = cfg.user;
      mode = "0400";
      content = ''
        OMP_WEB_PASSWORD=${config.sops.placeholder."${cfg.passwordSecret}"}
      '';
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
