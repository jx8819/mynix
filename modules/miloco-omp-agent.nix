# miloco-omp-agent — Miloco agent webhook → OMP bridge（仅米家设备/场景工具）
#
# 架构：Miloco backend（内置 WebhookAdapter）--HTTP--> 本服务 --stdio JSONL--> `omp --mode rpc`
#
# 能力边界（这是本模块存在的理由）：
#   * OMP 子进程以 --no-tools --no-extensions --no-skills --no-rules --no-lsp 启动，
#     没有 shell / 文件 / 网络 / 代码工具；
#   * 它唯一的工具是本服务注册的 miot_* 宮主工具，全部代理到 Miloco 的米家 API；
#   * tools.devicePolicy 在本进程内强制执行，命中 denyDids 的设备在发 HTTP 前就被拒绝。
#
# 机制完全公开；bearer / token / API Key 等私密值全部由调用方 options 指到文件路径。
{ config, lib, pkgs, ... }:

let
  cfg = config.services.miloco-omp-agent;

  systemPromptFile = pkgs.writeText "miloco-omp-system-prompt.txt" cfg.systemPrompt;

  configFile = pkgs.writeText "miloco-omp-agent-config.json" (builtins.toJSON {
    listen = {
      address = cfg.listenAddress;
      port = cfg.port;
    };
    allowedSourceIps = cfg.allowedSourceIps;
    authBearerFile = cfg.authBearerFile;
    miloco = {
      baseUrl = cfg.miloco.baseUrl;
      tokenFile = cfg.miloco.tokenFile;
    };
    omp = {
      command = "${cfg.ompPackage}/bin/omp";
      model = cfg.model;
      thinking = cfg.thinking;
      cwd = "${cfg.stateDir}/cwd";
      sessionDir = "${cfg.stateDir}/sessions";
      stateDir = cfg.stateDir;
      home = "${cfg.stateDir}/home";
      systemPromptFile = "${systemPromptFile}";
      # 独立 PI_CODING_AGENT_DIR：认证/设置/会话与管理员日常 omp 完全隔离
      env = {
        PI_CODING_AGENT_DIR = "${cfg.stateDir}/omp-agent";
      };
      apiKeyEnv = {
        ${cfg.apiKeyEnvVar} = cfg.apiKeyFile;
      };
      extraArgs = cfg.extraArgs;
    };
    tools = {
      readOnly = cfg.readOnly;
      devicePolicy = {
        inherit (cfg.devicePolicy) allowAll allowDids denyDids;
      };
    };
    defaultTurnTimeoutMs = cfg.defaultTurnTimeoutMs;
  });

  subdirs = [ "" "cwd" "home" "sessions" "tmp" "traces" "omp-agent" ];
in
{
  options.services.miloco-omp-agent = {
    enable = lib.mkEnableOption "Miloco agent webhook → OMP bridge（只给米家设备/场景工具）";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../pkgs/miloco-omp-agent { };
      defaultText = lib.literalExpression "pkgs.callPackage ../pkgs/miloco-omp-agent { }";
      description = "桥接守护进程包。";
    };

    ompPackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.unstable.omp or pkgs.omp;
      defaultText = lib.literalExpression "pkgs.unstable.omp or pkgs.omp";
      description = "提供 `omp` 可执行文件的包（桥接进程 spawn 的受限 Agent）。";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "agent";
      description = "运行服务的用户（需能读 apiKeyFile / tokenFile / authBearerFile）。";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "agent";
      description = "运行服务的组。";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "webhook 监听地址。Miloco 在别的主机/容器上时填本机 LAN 地址。";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 18125;
      description = "webhook 监听端口（Miloco 的 agent.webhook_url 指向它）。";
    };

    allowedSourceIps = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "10.10.10.17" ];
      description = "允许调用 webhook 的源 IP 白名单；空列表表示不限制（仍需 bearer）。";
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/miloco-omp-agent";
      description = "服务可写状态目录（OMP 会话/缓存/trace 都在这里）。";
    };

    authBearerFile = lib.mkOption {
      type = lib.types.str;
      example = "/mnt/Media/Settings/agent/omp/miloco-omp/secrets/webhook_bearer";
      description = "webhook bearer 令牌文件；须与 Miloco 的 agent.auth_bearer 一致。";
    };

    miloco = {
      baseUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:1810";
        description = "Miloco backend 地址（宿主工具经它访问米家 API）。";
      };
      tokenFile = lib.mkOption {
        type = lib.types.str;
        example = "/mnt/Media/Settings/agent/omp/miloco-omp/secrets/miloco_token";
        description = "Miloco server token 文件（Authorization: Bearer …）。";
      };
    };

    apiKeyFile = lib.mkOption {
      type = lib.types.str;
      example = "/mnt/Media/Settings/agent/omp/miloco-omp/secrets/mimo_api_key";
      description = "OMP 模型供应商 API Key 文件内容，注入到 apiKeyEnvVar 指定的环境变量。";
    };

    apiKeyEnvVar = lib.mkOption {
      type = lib.types.str;
      default = "XIAOMI_TOKEN_PLAN_CN_API_KEY";
      description = "OMP 模型供应商对应的环境变量名（按 OMP providers 文档）。";
    };

    model = lib.mkOption {
      type = lib.types.str;
      default = "xiaomi-token-plan-cn/mimo-v2.6-pro";
      description = "OMP 子进程使用的模型（provider/model-id）。";
    };

    thinking = lib.mkOption {
      type = lib.types.str;
      default = "off";
      description = "思考等级：off|minimal|low|medium|high。家庭设备控制默认求快。";
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "追加给 `omp --mode rpc` 的额外参数。";
    };

    systemPrompt = lib.mkOption {
      type = lib.types.lines;
      default = ''
        你是 Miloco 家庭智能体，负责查询和控制这个家庭的米家设备与场景。
        你只有 miot_* 系列工具：没有 shell、文件、网络、代码能力，也不需要它们。

        工作规则：
        1. 只做工具能做到的事；没执行过的动作绝不能说成已执行。
        2. 控制设备前先用 miot_device_spec / miot_device_status 确认 iid 与当前取值，再用准确的 iid 控制。
        3. 需要列场景时用 miot_home_overview 取 scene id，再用 miot_scene_trigger 触发。
        4. 工具报错时如实转述原因，不要编造成功。
        5. 回复用简短中文，面向家庭用户；列出你实际调用过的设备与动作。
      '';
      description = "OMP 子进程的系统提示（覆盖默认 coding-assistant 提示）。";
    };

    readOnly = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "只读模式：不注册 miot_device_control / miot_scene_trigger。";
    };

    devicePolicy = {
      allowAll = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "允许访问全部米家设备；false 时只允许 allowDids。";
      };
      allowDids = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = "allowAll=false 时的设备白名单（did）。";
      };
      denyDids = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        description = "硬拒绝的设备（did），优先于 allowAll；用于家里规定不许碰的设备。";
      };
    };

    defaultTurnTimeoutMs = lib.mkOption {
      type = lib.types.int;
      default = 60000;
      description = "Miloco 未给 timeoutMs 时的单 turn 等待上限（毫秒）。";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "在防火墙放行 port。";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = map
      (d: "d ${cfg.stateDir}${lib.optionalString (d != "") "/${d}"} 0700 ${cfg.user} ${cfg.group} -")
      subdirs;

    systemd.services.miloco-omp-agent = {
      description = "Miloco agent webhook → OMP bridge (Mi Home device tools only)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.stateDir;
        ExecStart = "${cfg.package}/bin/miloco-omp-agent --config ${configFile}";
        Restart = "on-failure";
        RestartSec = "5s";
        UMask = "0077";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir ];
        PrivateTmp = true;
        RestrictSUIDSGID = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
        LockPersonality = true;
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
