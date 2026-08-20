# =============================================================================
# Lab-in-a-Box
# =============================================================================
# `make` on its own prints every available target.
#
# Requires GNU make and bash. Both ship with macOS and Linux; on Windows they
# come with Git for Windows (use Git Bash) or with WSL. Everything here is a
# thin, readable wrapper around docker compose — if make is unavailable, the
# equivalent command is printed in the README for every target.
# =============================================================================

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Recipes that only orchestrate other commands do not need a sub-shell trace.
MAKEFLAGS += --no-print-directory

# -----------------------------------------------------------------------------
# Configuration
#
# Every variable can be overridden on the command line:
#   make up GPU=1
#   make logs SERVICE=keycloak
#   make restore BACKUP=20260803-141500
# -----------------------------------------------------------------------------
GPU        ?= 0
SERVICE    ?=
# Identity targets. FORMAT defaults to text; the rbac-* and access-review-*
# targets also accept json.
FORMAT     ?= text
OTHER      ?=
PERMISSION ?=
SUITE      ?= all
# Access review campaign targets.
CAMPAIGN   ?=
SCOPE      ?= all
ENTITLEMENT ?=
DECISION   ?=
NOTE       ?=
REVIEWER   ?=
BACKUP     ?=
PROFILES   ?=
FORCE      ?= 0
ASSUME_YES ?= 0

COMPOSE_BASE := docker compose -f docker-compose.yml
ifeq ($(GPU),1)
COMPOSE_BASE += -f compose/overrides/gpu.yml
endif

# PROFILES is comma-separated on the command line but must become one
# `--profile` flag per value. COMMA is a variable because a bare comma inside
# $(subst ...) would be parsed as an argument separator.
COMMA := ,
SPACE := $(subst x,,x) $(subst x,,x)
ifneq ($(PROFILES),)
COMPOSE_BASE += $(foreach p,$(subst $(COMMA),$(SPACE),$(PROFILES)),--profile $(p))
endif

COMPOSE := $(COMPOSE_BASE)

# Colours. On by default; disabled by the NO_COLOR convention or when running
# in CI. Deliberately not auto-detected from a tty — make evaluates $(shell)
# with its output attached to a pipe, so tty detection at parse time always
# reports "not a terminal" and would silently disable colour everywhere.
ifneq ($(or $(NO_COLOR),$(CI)),)
BOLD  :=
DIM   :=
GREEN :=
CYAN  :=
YELL  :=
RESET :=
else
BOLD  := \033[1m
DIM   := \033[2m
GREEN := \033[32m
CYAN  := \033[36m
YELL  := \033[33m
RESET := \033[0m
endif

.PHONY: help up down restart clean logs ps health creds secrets hooks backup \
        restore update pull validate lint docs shell https-on https-off version \
        jml-join jml-move jml-leave jml-show jml-test \
        rbac-show rbac-diff rbac-who-can \
        access-review-create access-review-show access-review-list \
        access-review-decide access-review-complete access-review-cancel \
        access-review-remediate access-review-test

# =============================================================================
# Help
# =============================================================================

help: ## Show this help
	@printf '\n$(BOLD)Lab-in-a-Box$(RESET)  $(DIM)— self-hosted AI, infrastructure and identity lab$(RESET)\n\n'
	@printf '$(BOLD)Usage:$(RESET)  make $(CYAN)<target>$(RESET) [VAR=value]\n\n'
	@printf '$(BOLD)Targets:$(RESET)\n'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-12s$(RESET) %s\n", $$1, $$2}'
	@printf '\n$(BOLD)Variables:$(RESET)\n'
	@printf '  $(CYAN)GPU=1$(RESET)         Enable NVIDIA passthrough for Ollama\n'
	@printf '  $(CYAN)PROFILES=$(RESET)     Optional services, comma-separated: qdrant, watchtower\n'
	@printf '  $(CYAN)SERVICE=$(RESET)      Limit logs/restart/shell to one service\n'
	@printf '  $(CYAN)BACKUP=$(RESET)       Which backup to restore (default: the newest)\n'
	@printf '  $(CYAN)FORCE=1$(RESET)       Regenerate credentials that already exist\n'
	@printf '\n$(BOLD)First run:$(RESET)\n'
	@printf '  $(GREEN)make secrets && make up$(RESET)\n\n'

# =============================================================================
# Lifecycle
# =============================================================================

up: ## Start the lab (generates credentials on first run)
	@if [[ ! -f .env ]]; then \
		printf '$(YELL)No .env found — generating credentials first.$(RESET)\n\n'; \
		bash scripts/generate-secrets.sh; \
		printf '\n'; \
	fi
	@$(MAKE) _https-mode
	@printf '$(BOLD)Starting the lab$(RESET)\n'
	@$(COMPOSE) up -d --remove-orphans
	@printf '\n$(GREEN)✓$(RESET) Containers started.\n'
	@printf '  $(DIM)First boot pulls images and downloads a language model —$(RESET)\n'
	@printf '  $(DIM)give it a few minutes before everything reports healthy.$(RESET)\n\n'
	@printf '  Watch progress:  $(CYAN)make logs$(RESET)\n'
	@printf '  Check status:    $(CYAN)make health$(RESET)\n'
	@printf '  Open the lab:    $(CYAN)https://%s$(RESET)\n\n' "$$(. scripts/lib/common.sh && lab_domain)"

down: ## Stop the lab, keeping all data
	@printf '$(BOLD)Stopping the lab$(RESET)\n'
	@$(COMPOSE) down --remove-orphans
	@printf '$(GREEN)✓$(RESET) Stopped. Volumes are intact — $(CYAN)make up$(RESET) resumes where you left off.\n'

restart: ## Restart everything, or one SERVICE
	@if [[ -n "$(SERVICE)" ]]; then \
		printf '$(BOLD)Restarting $(SERVICE)$(RESET)\n'; \
		$(COMPOSE) restart $(SERVICE); \
	else \
		printf '$(BOLD)Restarting the lab$(RESET)\n'; \
		$(COMPOSE) restart; \
	fi
	@printf '$(GREEN)✓$(RESET) Restarted.\n'

clean: ## Delete ALL lab data — volumes, containers, networks (asks first)
	@printf '$(YELL)This deletes every volume in the lab:$(RESET)\n'
	@printf '  databases, Keycloak realms, Git repositories, object storage,\n'
	@printf '  dashboards, chat history and downloaded models.\n\n'
	@printf '$(DIM)Credentials in .env and secrets/local/ are NOT deleted.$(RESET)\n\n'
	@if [[ "$(ASSUME_YES)" == "1" ]]; then \
		printf '$(DIM)ASSUME_YES=1 — proceeding.$(RESET)\n'; \
	else \
		read -r -p "$$(printf '$(YELL)Type '\''delete'\'' to confirm: $(RESET)')" reply; \
		[[ "$$reply" == "delete" ]] || { printf 'Cancelled.\n'; exit 1; }; \
	fi
	@$(COMPOSE) down --volumes --remove-orphans
	@printf '$(GREEN)✓$(RESET) All lab data removed. $(CYAN)make up$(RESET) starts fresh.\n'

# =============================================================================
# Observation
# =============================================================================

logs: ## Follow logs for everything, or one SERVICE
	@$(COMPOSE) logs --follow --tail 100 $(SERVICE)

ps: ## List containers and their health
	@$(COMPOSE) ps

health: ## Check every container, init job and HTTP route
	@bash scripts/health-check.sh

creds: ## Print all URLs and credentials
	@bash scripts/show-credentials.sh

shell: ## Open a shell inside SERVICE (e.g. make shell SERVICE=postgres)
	@if [[ -z "$(SERVICE)" ]]; then \
		printf 'Specify a service: $(CYAN)make shell SERVICE=postgres$(RESET)\n\n'; \
		$(COMPOSE) config --services | sort | sed 's/^/  /'; \
		exit 1; \
	fi
	@$(COMPOSE) exec $(SERVICE) sh -c 'command -v bash >/dev/null && exec bash || exec sh'

# =============================================================================
# Identity lifecycle  (Joiner / Mover / Leaver)
#
# Role profiles live in identity/profiles.json. The engine runs in a container,
# so none of this needs Python, curl or a service CLI on the host.
# =============================================================================

# `USER` is exported by the shell on macOS and Linux, and make imports the
# environment as variables. Without this guard, `make jml-leave` with no USER=
# would inherit the operator's own OS account name, sail past the usage check,
# and attempt to offboard whoever is logged in.
#
# `$(origin USER)` distinguishes the two sources: a command-line assignment
# reports "command line" and is kept; an inherited one reports "environment"
# and is cleared so the usage message fires as intended.
ifeq ($(origin USER),environment)
USER :=
endif

jml-join: ## Provision an identity (USER=erin ROLE=developer)
	@if [[ -z "$(USER)" || -z "$(ROLE)" ]]; then \
		printf 'Usage: $(CYAN)make jml-join USER=erin ROLE=developer$(RESET)\n'; \
		printf 'Profiles: $(DIM)developer, platform-admin, security, contractor$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/jml.sh join --user "$(USER)" --role "$(ROLE)"

jml-move: ## Move an identity between profiles (USER=erin FROM=contractor TO=developer)
	@if [[ -z "$(USER)" || -z "$(FROM)" || -z "$(TO)" ]]; then \
		printf 'Usage: $(CYAN)make jml-move USER=erin FROM=contractor TO=developer$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/jml.sh move --user "$(USER)" --from "$(FROM)" --to "$(TO)"

jml-leave: ## Offboard an identity, revoking access everywhere (USER=erin)
	@if [[ -z "$(USER)" ]]; then \
		printf 'Usage: $(CYAN)make jml-leave USER=erin$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/jml.sh leave --user "$(USER)"

jml-show: ## Print an identity's effective access across all services (USER=erin)
	@if [[ -z "$(USER)" ]]; then \
		printf 'Usage: $(CYAN)make jml-show USER=erin$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/jml.sh show --user "$(USER)"

jml-test: ## Run identity tests (SUITE=lifecycle|rbac|access-review|scim|all)
	@bash scripts/test-identity.sh $(SUITE)

# -----------------------------------------------------------------------------
# RBAC simulator — read-only. Answers "what can this person actually reach?"
# -----------------------------------------------------------------------------

rbac-show: ## Effective access for an identity, with reasons (USER=erin [FORMAT=json])
	@if [[ -z "$(USER)" ]]; then \
		printf 'Usage: $(CYAN)make rbac-show USER=erin$(RESET) [FORMAT=json]\n'; \
		exit 1; \
	fi
	@bash scripts/rbac.sh show --user "$(USER)" --format "$(FORMAT)"

rbac-diff: ## What one identity can reach that another cannot (USER=alice OTHER=bob)
	@if [[ -z "$(USER)" || -z "$(OTHER)" ]]; then \
		printf 'Usage: $(CYAN)make rbac-diff USER=alice OTHER=bob$(RESET) [FORMAT=json]\n'; \
		exit 1; \
	fi
	@bash scripts/rbac.sh diff --user "$(USER)" --other "$(OTHER)" --format "$(FORMAT)"

rbac-who-can: ## Every identity that can reach a resource (PERMISSION=vault:secret/data/security/*)
	@if [[ -z "$(PERMISSION)" ]]; then \
		printf 'Usage: $(CYAN)make rbac-who-can PERMISSION=vault:secret/data/security/*$(RESET)\n'; \
		printf 'Services: $(DIM)keycloak, vault, gitea, grafana$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/rbac.sh who-can --permission "$(PERMISSION)" --format "$(FORMAT)"

# -----------------------------------------------------------------------------
# Access review campaigns, a governance workflow built on the RBAC simulator.
# Discovery is read-only; only `remediate` changes live access, and only for
# items decided REVOKE, reusing the same JML adapters as the lifecycle
# commands. See docs/identity-governance.md.
# -----------------------------------------------------------------------------

access-review-create: ## Open a campaign (NAME=quarterly-q3 [SCOPE=all|profile:developer|user:erin])
	@if [[ -z "$(NAME)" ]]; then \
		printf 'Usage: $(CYAN)make access-review-create NAME=quarterly-q3$(RESET) [SCOPE=all|profile:<name>|user:<name>]\n'; \
		exit 1; \
	fi
	@bash scripts/access-review.sh create --name "$(NAME)" --scope "$(SCOPE)" --reviewer "$(REVIEWER)" --format "$(FORMAT)"

access-review-show: ## Show a campaign's items and decisions (CAMPAIGN=<id> [FORMAT=json])
	@if [[ -z "$(CAMPAIGN)" ]]; then \
		printf 'Usage: $(CYAN)make access-review-show CAMPAIGN=<id>$(RESET) [FORMAT=json]\n'; \
		exit 1; \
	fi
	@bash scripts/access-review.sh show --campaign "$(CAMPAIGN)" --format "$(FORMAT)"

access-review-list: ## List all campaigns
	@bash scripts/access-review.sh list --format "$(FORMAT)"

access-review-decide: ## Record a decision (CAMPAIGN=<id> USER=erin ENTITLEMENT="Gitea:team developers" DECISION=approve)
	@if [[ -z "$(CAMPAIGN)" || -z "$(USER)" || -z "$(ENTITLEMENT)" || -z "$(DECISION)" ]]; then \
		printf 'Usage: $(CYAN)make access-review-decide CAMPAIGN=<id> USER=erin ENTITLEMENT="Gitea:team developers" DECISION=approve$(RESET)\n'; \
		printf 'Decisions: $(DIM)approve, revoke, not-applicable$(RESET)  (NOTE=... optional, FORCE=1 to override an existing decision)\n'; \
		exit 1; \
	fi
	@bash scripts/access-review.sh decide --campaign "$(CAMPAIGN)" --user "$(USER)" --entitlement "$(ENTITLEMENT)" \
		--decision "$(DECISION)" --note "$(NOTE)" --reviewer "$(REVIEWER)" $(if $(filter 1,$(FORCE)),--force,)

access-review-complete: ## Close a campaign (CAMPAIGN=<id>, refuses undecided items unless FORCE=1)
	@if [[ -z "$(CAMPAIGN)" ]]; then \
		printf 'Usage: $(CYAN)make access-review-complete CAMPAIGN=<id>$(RESET) [FORCE=1]\n'; \
		exit 1; \
	fi
	@bash scripts/access-review.sh complete --campaign "$(CAMPAIGN)" --format "$(FORMAT)" $(if $(filter 1,$(FORCE)),--force,)

access-review-cancel: ## Cancel a campaign (CAMPAIGN=<id>)
	@if [[ -z "$(CAMPAIGN)" ]]; then \
		printf 'Usage: $(CYAN)make access-review-cancel CAMPAIGN=<id>$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/access-review.sh cancel --campaign "$(CAMPAIGN)"

access-review-remediate: ## Act on revoke decisions (CAMPAIGN=<id>); the only mutating campaign command
	@if [[ -z "$(CAMPAIGN)" ]]; then \
		printf 'Usage: $(CYAN)make access-review-remediate CAMPAIGN=<id>$(RESET)\n'; \
		exit 1; \
	fi
	@bash scripts/access-review.sh remediate --campaign "$(CAMPAIGN)" --format "$(FORMAT)"

access-review-test: ## Run the access review test suite against the running lab
	@bash scripts/test-identity.sh access-review

# -----------------------------------------------------------------------------
# SCIM provisioning. Keycloak serves SCIM; the long-running provisioner
# reconciles those identities into Gitea and Grafana automatically.
# -----------------------------------------------------------------------------

scim-token: ## Print a short-lived bearer token for the Keycloak SCIM API
	@bash scripts/scim.sh token

scim-status: ## Show downstream provisioning state and pending retries
	@docker compose --project-directory "$(CURDIR)" exec -T scim-provisioner \
		python3 engine/scim_worker.py status

scim-test: ## Run SCIM endpoint and downstream propagation tests
	@bash scripts/test-identity.sh scim

scim-conformance: ## Run the published SCIM 2.0 compliance suite (requires internet)
	@bash scripts/scim-conformance.sh

# =============================================================================
# Credentials
# =============================================================================

secrets: ## Generate random credentials into .env and secrets/local/
	@FORCE=$(FORCE) bash scripts/generate-secrets.sh

hooks: ## Install a pre-commit hook that blocks committing real credentials
	@bash scripts/install-hooks.sh

# =============================================================================
# Data
# =============================================================================

backup: ## Snapshot databases and volumes into backups/
	@bash scripts/backup.sh

restore: ## Restore a backup (BACKUP=<timestamp>, default: newest)
	@BACKUP=$(BACKUP) ASSUME_YES=$(ASSUME_YES) bash scripts/restore.sh

# =============================================================================
# Maintenance
# =============================================================================

pull: ## Download the latest images without applying them
	@printf '$(BOLD)Pulling images$(RESET)\n'
	@$(COMPOSE) pull --ignore-pull-failures
	@printf '$(GREEN)✓$(RESET) Images downloaded. Apply them with $(CYAN)make update$(RESET).\n'

update: ## Pull the latest images and recreate changed containers
	@printf '$(BOLD)Updating the lab$(RESET)\n'
	@$(COMPOSE) pull --ignore-pull-failures
	@$(COMPOSE) up -d --remove-orphans
	@printf '$(GREEN)✓$(RESET) Updated.\n'
	@printf '  $(DIM)Only containers whose image changed were recreated.$(RESET)\n'
	@printf '  $(DIM)Reclaim the superseded layers with: docker image prune$(RESET)\n'

# =============================================================================
# Quality
# =============================================================================

validate: ## Check that the compose project and every config file parse
	@bash scripts/validate.sh

lint: validate ## Alias for validate
	@true

docs: ## Regenerate the service catalogue and dependency graph from compose
	@bash scripts/generate-docs.sh

# =============================================================================
# TLS
# =============================================================================

https-on: ## Redirect all HTTP traffic to HTTPS
	@cp configs/traefik/optional/https-redirect.yml configs/traefik/dynamic/https-redirect.yml
	@printf '$(GREEN)✓$(RESET) HTTPS redirect enabled. $(DIM)Traefik picks it up within seconds — no restart.$(RESET)\n'

https-off: ## Stop redirecting HTTP to HTTPS
	@rm -f configs/traefik/dynamic/https-redirect.yml
	@printf '$(GREEN)✓$(RESET) HTTPS redirect disabled.\n'

# Applies whatever LAB_FORCE_HTTPS says in .env. Called by `up` so the setting
# takes effect without a separate command.
_https-mode:
	@if [[ "$$(. scripts/lib/common.sh && env_value LAB_FORCE_HTTPS false)" == "true" ]]; then \
		cp configs/traefik/optional/https-redirect.yml configs/traefik/dynamic/https-redirect.yml; \
	else \
		rm -f configs/traefik/dynamic/https-redirect.yml; \
	fi

# =============================================================================
# Misc
# =============================================================================

version: ## Print versions of the tools this lab depends on
	@printf '$(BOLD)Toolchain$(RESET)\n'
	@printf '  docker          %s\n' "$$(docker --version 2>/dev/null || echo 'not found')"
	@printf '  docker compose  %s\n' "$$(docker compose version --short 2>/dev/null || echo 'not found')"
	@printf '  make            %s\n' "$$(make --version 2>/dev/null | head -1 || echo 'not found')"
	@printf '  bash            %s\n' "$$(bash --version 2>/dev/null | head -1 || echo 'not found')"
