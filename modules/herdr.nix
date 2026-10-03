# Herdr — terminal workspace manager for AI coding agents
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.herdr;
  tomlFormat = pkgs.formats.toml { };

  knownUsers = lib.filterAttrs (
    name: _:
    builtins.hasAttr name config.users.users
    && config.users.users.${name}.enable
  ) cfg.users;

  userTmpfiles = name: userCfg:
    let
      nixosUser = config.users.users.${name};
      configDir = "${nixosUser.home}/.config/herdr";
      configPath = "${configDir}/config.toml";
      source =
        if userCfg.configFile != null then
          userCfg.configFile
        else
          tomlFormat.generate "herdr-${name}-config.toml" userCfg.settings;
    in
    {
      "${nixosUser.home}/.config".d = {
        mode = "0700";
        user = nixosUser.name;
        group = nixosUser.group;
      };

      "${configDir}".d = {
        mode = "0700";
        user = nixosUser.name;
        group = nixosUser.group;
      };

      "${configPath}"."L+" = {
        user = nixosUser.name;
        group = nixosUser.group;
        argument = lib.replaceStrings [ "%" ] [ "%%" ] (toString source);
      };
    };
in
{
  options.programs.herdr = {
    enable = lib.mkEnableOption "Herdr terminal workspace manager";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../pkgs/herdr { };
      defaultText = lib.literalExpression "pkgs.herdr";
      description = "The Herdr package to install system-wide.";
    };

    users = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          settings = lib.mkOption {
            type = tomlFormat.type;
            default = { };
            description = ''
              Herdr settings written to the user's config file as TOML.

              WARNING: The generated file is stored in the world-readable Nix
              store. Do not put passwords, tokens, or other secrets here. Use
              `configFile` for a runtime secret-backed file instead.
            '';
          };

          configFile = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            example = "/run/secrets-rendered/herdr-config.toml";
            description = ''
              Absolute runtime path to an existing Herdr TOML configuration.
              The path is used as an opaque symlink target; its contents are
              never imported into the Nix store. This supports paths produced
              by caller-provided secret management such as a sops template.
            '';
          };
        };
      });
      default = { };
      description = ''
        Per-user Herdr configuration. Each attribute name must identify a user
        declared in `users.users`; its config is linked at
        `<home>/.config/herdr/config.toml`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions =
      lib.mapAttrsToList (name: _: {
        assertion = builtins.hasAttr name config.users.users;
        message = "programs.herdr.users.${name} refers to an undefined NixOS user; define users.users.${name}.";
      }) cfg.users
      ++ lib.mapAttrsToList (name: _: {
        assertion =
          !builtins.hasAttr name config.users.users
          || config.users.users.${name}.enable;
        message = "programs.herdr.users.${name} refers to a disabled NixOS user; enable users.users.${name}.";
      }) cfg.users
      ++ lib.mapAttrsToList (name: userCfg: {
        assertion = userCfg.settings == { } || userCfg.configFile == null;
        message = "programs.herdr.users.${name}.settings and configFile are mutually exclusive; set only one.";
      }) cfg.users
      ++ lib.mapAttrsToList (name: userCfg: {
        assertion = userCfg.configFile == null || lib.hasPrefix "/" userCfg.configFile;
        message = "programs.herdr.users.${name}.configFile must be an absolute runtime path.";
      }) cfg.users;

    environment.systemPackages = [ cfg.package ];

    systemd.tmpfiles.settings."10-herdr" = lib.mkMerge (
      lib.mapAttrsToList userTmpfiles knownUsers
    );
  };
}
