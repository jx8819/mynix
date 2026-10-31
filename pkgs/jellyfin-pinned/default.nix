# Jellyfin 10.11.z 冻结构建：源码固定 jx8819/jellyfin fork 的 release-10.11.z，
# 构建工具链与 jellyfin-ffmpeg / jellyfin-web 全部来自 flake input `jellyfin-pin`
# 锁定的 nixpkgs（URL 内精确 rev），升级宿主 NixOS / nixpkgs 不改变本包闭包。
# 换 fork commit 时更新 forkRev 与源码 hash；csproj 的 NuGet 依赖若有变化，需按
# jellyfin-pin 对应 nixpkgs 里 pkgs/by-name/je/jellyfin/update.sh 的流程重建
# nuget-deps.json（本包直接复用冻结 nixpkgs 中的那份）。
{ fetchFromGitHub, pinnedPkgs }:

let
  # jx8819/jellyfin release-10.11.z（= v10.11.11）
  forkRev = "1fbd8739292cce610231be93daf43368733edf63";
in
pinnedPkgs.jellyfin.overrideAttrs (old: {
  src = fetchFromGitHub {
    owner = "jx8819";
    repo = "jellyfin";
    rev = forkRev;
    hash = "sha256-HCs4ZsutVoVH+bBZANjpPeMyV8e63Yemjg9DSr0R9zg=";
  };

  passthru = old.passthru // {
    inherit forkRev;
    pinnedNixpkgsRev = "a033e66ab94a6bedf2c3d58d93a36319d3cbbbaf";
  };

  meta = old.meta // {
    description = "Jellyfin Media Server built from the frozen 10.11.z fork with a pinned toolchain";
  };
})
