# SPDX-License-Identifier: GPL-2.0-only

ifndef OFA_DIR
$(error ksmbd-ofed requires OFA_DIR from the supplied OFED header export)
endif
ifndef K_BUILD
$(error ksmbd-ofed requires K_BUILD from the supplied kernel.dev)
endif
ifndef MODULE_SRC
$(error ksmbd-ofed requires MODULE_SRC pointing to fs/smb/server)
endif

# Read BACKPORT_INCLUDES just as the vendor's NFS/RDMA consumer does. Do not
# export this file's CONFIG_* assignments to the kernel's build: only the
# prepared kernel configuration and the two explicit SMB module flags apply.
include $(OFA_DIR)/configure.mk.kernel

.PHONY: modules
modules:
	$(MAKE) -C "$(K_BUILD)" M="$(MODULE_SRC)" \
		OFA_BACKPORT_INCLUDES='$(BACKPORT_INCLUDES)' \
		KBUILD_EXTRA_SYMBOLS='$(OFA_DIR)/Module.symvers' modules
