# ============================================================================
# Oracle -- build / verification gate
#
#   make check   -- the FULL gate (what CI runs):
#                     1. render the kernel hash manifest
#                     2. spawn a throwaway oracle into a temp dir
#                     3. deep setup_audit on the spawn
#                     4. schema-validating oracle_lint
#                     5. secret scan over the kernel template tree
#                     6. pytest (kernel suite + shell suite, stdlib-only)
# ============================================================================

PY ?= python3
PYTEST ?= $(PY) -m pytest

ROOT       := $(CURDIR)
PKG        := $(ROOT)/src/oracle_agent
KERNEL     := $(PKG)/assets/oracle-kernel
TOOLS      := $(KERNEL)/_tools
TMP_ROOT   := $(ROOT)/tmp.nosync/_make
SPAWN_ROOT := $(TMP_ROOT)/spawned

COMPANY  ?= Make Check Co
CODENAME ?= MAKECHECK
ADMIN    ?= Make Admin

export PYTHONPATH := $(ROOT)/src

.DEFAULT_GOAL := check

.PHONY: check manifest spawn audit lint secret test ci help clean

## check: run the full verification gate
check: manifest spawn audit lint secret test
	@echo ""
	@echo "================================================================"
	@echo " make check: PASS -- full gate green"
	@echo "================================================================"

## manifest: sha256 every _tools file -> .kernel-manifest.json
manifest:
	@echo "==> render kernel hash manifest"
	$(PY) -m oracle_agent.manifest --kernel "$(KERNEL)"

## spawn: spawn a throwaway oracle into a temp dir (rebuilt every run)
spawn:
	@echo "==> spawn throwaway oracle -> $(SPAWN_ROOT)"
	@rm -rf "$(SPAWN_ROOT)"
	@mkdir -p "$(TMP_ROOT)"
	$(PY) -m oracle_agent.spawn \
		--root "$(SPAWN_ROOT)" \
		--company-name "$(COMPANY)" \
		--codename "$(CODENAME)" \
		--admin-name "$(ADMIN)"

## audit: deep setup_audit on the spawned oracle
audit:
	@echo "==> setup_audit on spawned oracle"
	$(PY) "$(TOOLS)/setup_audit.py" "$(SPAWN_ROOT)"

## lint: schema-validating oracle_lint on the spawned oracle
lint:
	@echo "==> oracle_lint on spawned oracle"
	$(PY) "$(TOOLS)/oracle_lint.py" "$(SPAWN_ROOT)"

## secret: scan the SHIPPED kernel template tree for leaked credentials
secret:
	@echo "==> secret scan over shipped kernel template tree (tests/ excluded)"
	@cd "$(KERNEL)" && for entry in * .[!.]*; do \
		[ -e "$$entry" ] || continue ; \
		case "$$entry" in \
			tests|.pytest_cache|tmp.nosync) continue ;; \
		esac ; \
		echo "    scanning: $$entry" ; \
		$(PY) "$(TOOLS)/secret_scan.py" scan "$$entry" || exit 1 ; \
	done
	@echo "    (clean -- no secrets in shipped kernel content)"

## test: run the full pytest suite (kernel + shell, stdlib-only)
test:
	@echo "==> pytest (kernel + shell suites)"
	$(PYTEST) -q

## ci: alias for check
ci: check

## clean: remove the temp spawn and pytest caches
clean:
	@rm -rf "$(TMP_ROOT)" "$(ROOT)/.pytest_cache"
	@find "$(ROOT)" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

## help: list available targets
help:
	@grep -E '^## ' "$(firstword $(MAKEFILE_LIST))" | sed -e 's/^## /  /'
