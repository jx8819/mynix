# Jellyfin 声明式配置扩展：在上游 `services.jellyfin` 之上托管 encoding.xml / system.xml /
# network.xml 的选定字段，并默认改用冻结构建的 pkgs.jellyfin-pinned。
#
# 与上游 `services.jellyfin.forceEncodingConfig` 的取舍：该开关每次启动整文件重写
# encoding.xml（每轮产生 backup），且模板不含 TranscodingTempPath、QsvDevice 等字段。
# 这里只写入显式声明（非 null）的字段，其余字段继续由 Web UI 管理；null = 不托管。
# 字段默认值即 Jellyfin 10.11 的出厂默认，站点实例只声明自己的非默认设置。
{ config, pkgs, lib, ... }:

let
  cfg = config.services.jellyfin;

  xmlBool = b: if b then "true" else "false";

  # name=value 列表（value 为 XML 文本值），null 字段跳过；调用端逐个 escapeShellArg
  kvList = fields:
    lib.mapAttrsToList (name: value: "${name}=${toString value}")
      (lib.filterAttrs (_: v: v != null) fields);

  kvArgs = fields: lib.concatMapStringsSep " " lib.escapeShellArg (kvList fields);
  hasFields = fields: lib.filterAttrs (_: v: v != null) fields != { };

  encodingScalars = {
    TranscodingTempPath = cfg.encoding.transcodingTempPath;
    HardwareAccelerationType = cfg.encoding.hardwareAccelerationType;
    VaapiDevice = cfg.encoding.vaapiDevice;
    QsvDevice = cfg.encoding.qsvDevice;
    EnableTonemapping = lib.mapNullable xmlBool cfg.encoding.enableTonemapping;
    EnableDecodingColorDepth10HevcRext =
      lib.mapNullable xmlBool cfg.encoding.enableDecodingColorDepth10HevcRext;
    EnableDecodingColorDepth12HevcRext =
      lib.mapNullable xmlBool cfg.encoding.enableDecodingColorDepth12HevcRext;
  };

  serverScalars = {
    UICulture = cfg.server.uiCulture;
    PreferredMetadataLanguage = cfg.server.preferredMetadataLanguage;
    MetadataCountryCode = cfg.server.metadataCountryCode;
    LibraryScanFanoutConcurrency = lib.mapNullable toString cfg.server.libraryScanFanoutConcurrency;
  };

  networkScalars = {
    EnableRemoteAccess = lib.mapNullable xmlBool cfg.network.enableRemoteAccess;
    AutoDiscovery = lib.mapNullable xmlBool cfg.network.autoDiscovery;
  };

  seedScript = pkgs.writeShellScript "jellyfin-config-seed" ''
    set -euo pipefail
    xmlstarlet=${pkgs.xmlstarlet}/bin/xmlstarlet
    configDir=${lib.escapeShellArg cfg.configDir}

    ensure() {
      local file="$1" root="$2"
      if [ ! -f "$file" ]; then
        printf '<?xml version="1.0" encoding="utf-8"?>\n<%s />\n' "$root" > "$file"
      fi
    }

    # name=value 逐对写入：节点存在则更新，缺失则插入
    seed_scalars() {
      local file="$1" root="$2"; shift 2
      if [ "$#" -eq 0 ]; then return 0; fi
      local args=() kv name value
      for kv in "$@"; do
        name="''${kv%%=*}"
        value="''${kv#*=}"
        args+=(-u "/$root/$name" -v "$value")
        args+=(-s "/$root[not($name)]" -t elem -n "$name" -v "$value")
      done
      "$xmlstarlet" ed --inplace "''${args[@]}" "$file"
    }

    # 列表字段：保证父节点存在后整组重建 <string> 子节点
    seed_list() {
      local file="$1" root="$2" name="$3"; shift 3
      local args=(-s "/$root[not($name)]" -t elem -n "$name")
      args+=(-d "/$root/$name/*")
      local v
      for v in "$@"; do
        args+=(-s "/$root/$name" -t elem -n string -v "$v")
      done
      "$xmlstarlet" ed --inplace "''${args[@]}" "$file"
    }

    encodingXml="$configDir/encoding.xml"
    ensure "$encodingXml" EncodingOptions
    seed_scalars "$encodingXml" EncodingOptions ${kvArgs encodingScalars}
    ${lib.optionalString (cfg.encoding.hardwareDecodingCodecs != null) ''
      seed_list "$encodingXml" EncodingOptions HardwareDecodingCodecs ${lib.concatMapStringsSep " " lib.escapeShellArg cfg.encoding.hardwareDecodingCodecs}
    ''}

    systemXml="$configDir/system.xml"
    ensure "$systemXml" ServerConfiguration
    seed_scalars "$systemXml" ServerConfiguration ${kvArgs serverScalars}

    networkXml="$configDir/network.xml"
    ensure "$networkXml" NetworkConfiguration
    seed_scalars "$networkXml" NetworkConfiguration ${kvArgs networkScalars}
  '';
in
{
  options.services.jellyfin = {
    encoding = {
      transcodingTempPath = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "/mnt/nvme-cache/jellyfin/cache/transcodes";
        description = "Transcoding temp directory written to encoding.xml. null leaves it unmanaged.";
      };
      hardwareAccelerationType = lib.mkOption {
        type = lib.types.nullOr (lib.types.enum [
          "none"
          "amf"
          "qsv"
          "nvenc"
          "v4l2m2m"
          "vaapi"
          "rkmpp"
        ]);
        default = null;
        description = "Hardware acceleration method written to encoding.xml. null leaves it unmanaged.";
      };
      vaapiDevice = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/dev/dri/renderD128";
        description = "VA-API render node written to encoding.xml. null leaves it unmanaged.";
      };
      qsvDevice = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/dev/dri/renderD256";
        description = "QSV render node written to encoding.xml. null leaves it unmanaged.";
      };
      enableTonemapping = lib.mkOption {
        type = lib.types.nullOr lib.types.bool;
        default = null;
        description = "HDR to SDR tone mapping written to encoding.xml. null leaves it unmanaged.";
      };
      enableDecodingColorDepth10HevcRext = lib.mkOption {
        type = lib.types.nullOr lib.types.bool;
        default = null;
        description = "10-bit HEVC RExt hardware decoding written to encoding.xml. null leaves it unmanaged.";
      };
      enableDecodingColorDepth12HevcRext = lib.mkOption {
        type = lib.types.nullOr lib.types.bool;
        default = null;
        description = "12-bit HEVC RExt hardware decoding written to encoding.xml. null leaves it unmanaged.";
      };
      hardwareDecodingCodecs = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str);
        default = null;
        example = [ "h264" "hevc" "vc1" ];
        description = "Hardware decoding codecs written to encoding.xml. null leaves it unmanaged.";
      };
    };

    server = {
      uiCulture = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "zh-CN";
        description = "Web UI culture written to system.xml. null leaves it unmanaged.";
      };
      preferredMetadataLanguage = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "zh";
        description = "Preferred metadata language written to system.xml. null leaves it unmanaged.";
      };
      metadataCountryCode = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "CN";
        description = "Metadata country code written to system.xml. null leaves it unmanaged.";
      };
      libraryScanFanoutConcurrency = lib.mkOption {
        type = lib.types.nullOr lib.types.ints.unsigned;
        default = null;
        example = 2;
        description = "Library scan fan-out concurrency written to system.xml. null leaves it unmanaged.";
      };
    };

    network = {
      enableRemoteAccess = lib.mkOption {
        type = lib.types.nullOr lib.types.bool;
        default = null;
        description = "Remote access switch written to network.xml. null leaves it unmanaged.";
      };
      autoDiscovery = lib.mkOption {
        type = lib.types.nullOr lib.types.bool;
        default = null;
        description = "DLNA/SSDP auto discovery written to network.xml. null leaves it unmanaged.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # 冻结构建的 Jellyfin（fork 源码 + pin 死的 ffmpeg/web/工具链）是默认包；
    # 需要上游包时显式覆盖 services.jellyfin.package。
    services.jellyfin.package = lib.mkDefault pkgs.jellyfin-pinned;

    systemd.services.jellyfin = {
      # 排在上游 preStart（hardwareAcceleration 开启时生成 encoding.xml 模板）之后
      preStart = lib.mkAfter ''
        ${seedScript}
      '';
      unitConfig.RequiresMountsFor =
        [ cfg.dataDir ]
        ++ lib.optional (cfg.encoding.transcodingTempPath != null) cfg.encoding.transcodingTempPath;
      serviceConfig = {
        ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
        ExecStop = "${pkgs.coreutils}/bin/kill -QUIT $MAINPID";
      };
    };

    systemd.tmpfiles.rules = lib.mkIf (cfg.encoding.transcodingTempPath != null) [
      "d ${cfg.encoding.transcodingTempPath} 0700 ${cfg.user} ${cfg.group} -"
    ];
  };
}
