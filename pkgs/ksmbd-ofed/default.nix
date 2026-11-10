{ lib, kernel, ofed }:

assert lib.assertMsg
  (kernel ? commonMakeFlags && kernel ? moduleBuildDependencies && kernel ? dev)
  "ksmbd-ofed requires a Nixpkgs kernel with prepared external-module headers";
assert lib.assertMsg (ofed ? override)
  "ksmbd-ofed requires the overridable mlnx-ofed-kernel package";

let
  kernelBuild = "${kernel.dev}/lib/modules/${kernel.modDirVersion}/build";
  kernelModuleMakeFlags = kernel.commonMakeFlags ++ [
    "KBUILD_OUTPUT=${kernelBuild}"
    "DEPMOD=true"
  ];
  # Match the OFED consumers' header export, without adding its modules to out.
  ofedHeaders = ofed.override {
    inherit kernel;
    copySource = true;
  };
  ofaDir = "${ofedHeaders}/src/ofa_kernel/${kernel.modDirVersion}";
  moduleRelativePath = "lib/modules/${kernel.modDirVersion}/updates/ksmbd.ko";
  metadataRelativePath = "nix-support/ksmbd-ofed";
in
kernel.stdenv.mkDerivation (finalAttrs: {
  pname = "ksmbd-ofed";
  version = "${kernel.version}-${ofed.version}";

  # The dev output deliberately omits C sources. Apply the same source patches,
  # then use only fs/smb/server as an external module against the prepared dev.
  inherit (kernel) src;
  patches = (kernel.patches or [ ]) ++ [
    ./0001-ksmbd-preserve-transform-header-in-smb-direct-writes.patch
  ];
  patchFlags = kernel.patchFlags or [ "-p1" ];
  prePatch = kernel.prePatch or "";
  postPatch = (kernel.postPatch or "") + ''
    if [ -e fs/smb/server/Kbuild ]; then
      echo "ksmbd-ofed: an upstream fs/smb/server/Kbuild is not supported" >&2
      exit 1
    fi
    for file in fs/smb/server/Makefile fs/smb/server/transport_rdma.c \
      fs/smb/common/smbdirect/smbdirect.h \
      fs/smb/common/smbdirect/smbdirect_pdu.h \
      fs/smb/common/smbdirect/smbdirect_socket.h; do
      if [ ! -f "$file" ]; then
        echo "ksmbd-ofed: unsupported kernel source layout: missing $file" >&2
        exit 1
      fi
    done
    cp ${./Kbuild} fs/smb/server/Kbuild
  '';

  nativeBuildInputs = kernel.moduleBuildDependencies;
  inherit (kernel) hardeningDisable;
  enableParallelBuilding = true;
  dontConfigure = true;
  # Keep full DWARF in the exact installed ELF for independent ABI comparison.
  dontStrip = true;

  makeFlags = kernelModuleMakeFlags ++ [
    "K_BUILD=${kernelBuild}"
    "OFA_DIR=${ofaDir}"
    "CONFIG_SMB_SERVER=m"
    "CONFIG_SMB_SERVER_SMBDIRECT=y"
  ];

  buildPhase = ''
    runHook preBuild

    for file in "${kernelBuild}/.config" \
      "${kernelBuild}/Module.symvers" \
      "${kernelBuild}/include/generated/autoconf.h" \
      "${ofaDir}/configure.mk.kernel" \
      "${ofaDir}/Module.symvers" \
      "${ofaDir}/compat/config.h" \
      "${ofaDir}/include/generated/autoconf.h" \
      "${ofaDir}/include/linux/compat-2.6.h" \
      "${ofaDir}/include/rdma/ib_verbs.h" \
      "${ofaDir}/include/rdma/rdma_cm.h" \
      "${ofaDir}/include/rdma/rw.h"; do
      if [ ! -s "$file" ]; then
        echo "ksmbd-ofed: missing prepared kernel/OFED build input: $file" >&2
        exit 1
      fi
    done

    if ! grep -Fxq 'KVERSION=${kernel.modDirVersion}' "${ofaDir}/configure.mk.kernel" \
      || ! grep -Fxq 'KSRC_OBJ=${kernelBuild}' "${ofaDir}/configure.mk.kernel"; then
      echo "ksmbd-ofed: OFED headers were not configured against the supplied kernel.dev" >&2
      exit 1
    fi

    require_config() {
      if ! grep -Eq "$1" "${kernelBuild}/.config"; then
        echo "ksmbd-ofed: the supplied kernel must already enable $2" >&2
        exit 1
      fi
    }
    require_config '^CONFIG_MODULES=y$' 'CONFIG_MODULES=y'
    require_config '^CONFIG_SMB_SERVER=m$' 'CONFIG_SMB_SERVER=m'
    require_config '^CONFIG_INFINIBAND=(y|m)$' 'CONFIG_INFINIBAND'
    require_config '^CONFIG_INFINIBAND_ADDR_TRANS=y$' 'CONFIG_INFINIBAND_ADDR_TRANS=y'
    require_config '^CONFIG_SG_POOL=y$' 'CONFIG_SG_POOL=y'
    if grep -Fxq 'CONFIG_MODULE_SIG_FORCE=y' "${kernelBuild}/.config"; then
      echo "ksmbd-ofed: a kernel requiring signed modules needs an explicit module-signing implementation" >&2
      exit 1
    fi

    make -f ${./Makefile} -j"$NIX_BUILD_CORES" ${lib.escapeShellArgs finalAttrs.makeFlags} \
      "MODULE_SRC=$PWD/fs/smb/server" modules

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    install -Dm644 fs/smb/server/ksmbd.ko "$out/${moduleRelativePath}"
    mkdir -p "$out/etc/depmod.d" "$out/${metadataRelativePath}"
    printf '%s\n' 'override ksmbd * updates' > "$out/etc/depmod.d/ksmbd-ofed.conf"

    install -m644 fs/smb/server/.transport_rdma.o.cmd \
      "$out/${metadataRelativePath}/transport_rdma.o.cmd"
    install -m644 fs/smb/server/.ksmbd.ko.cmd \
      "$out/${metadataRelativePath}/ksmbd.ko.cmd"
    install -m644 fs/smb/server/Module.symvers \
      "$out/${metadataRelativePath}/Module.symvers"
    install -m644 "${kernelBuild}/.config" \
      "$out/${metadataRelativePath}/kernel.config"
    install -m644 "${ofaDir}/configure.mk.kernel" \
      "$out/${metadataRelativePath}/ofed.configure.mk.kernel"
    install -m644 "${ofaDir}/compat/config.h" \
      "$out/${metadataRelativePath}/ofed.compat.config.h"
    install -m644 "${ofaDir}/include/generated/autoconf.h" \
      "$out/${metadataRelativePath}/ofed.autoconf.h"
    printf '%s\n' ${lib.escapeShellArg (builtins.toJSON {
      kernelVersion = kernel.version;
      moduleDirectoryVersion = kernel.modDirVersion;
      kernelDev = toString kernel.dev;
      kernelSource = toString kernel.src;
      ofed = toString ofed;
      ofedHeaders = toString ofedHeaders;
      module = moduleRelativePath;
      smbDirect = true;
    })} > "$out/${metadataRelativePath}/build-info.json"

    runHook postInstall
  '';

  passthru = {
    inherit kernel ofed ofedHeaders moduleRelativePath metadataRelativePath;
    modulePath = "${finalAttrs.finalPackage}/${moduleRelativePath}";
    metadataPath = "${finalAttrs.finalPackage}/${metadataRelativePath}";
  };

  meta = {
    description = "ksmbd with SMB Direct built against the supplied Mellanox OFED kernel headers";
    license = lib.licenses.gpl2Only;
    platforms = lib.platforms.linux;
  };
})
