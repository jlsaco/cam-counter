# ─────────────────────────────────────────────────────────────────────────────
# terraform/Makefile — ciclo de vida de la infraestructura cam-counter.
#
# QUIÉN EJECUTA QUÉ:
#   - `fmt-check` / `validate` / `tflint`  → lo único que corre GitHub Actions CI
#     (ubuntu-latest, SIN credenciales AWS, SIN plan/apply).
#   - `bootstrap` / `plan` / `apply` / `destroy` → los ejecuta el RUNNER MAD de
#     forma AUTÓNOMA con las credenciales de SU ENTORNO (jamás commiteadas),
#     SIEMPRE tras inspeccionar que el `plan` es estrictamente ADITIVO (F1).
#     El CI NUNCA aplica.
#
# F1 (state aditivo/monótono): aplica SÓLO desde la rama apilada más alta con
# todo el HCL acumulado; nunca reaplicar una inferior tras una superior.
# ─────────────────────────────────────────────────────────────────────────────

TF_PROD   := environments/prod
TF_MODULE := modules/state-backend

# tflint encuentra esta config aunque se invoque con --chdir (ruta absoluta).
export TFLINT_CONFIG_FILE := $(abspath .tflint.hcl)

.DEFAULT_GOAL := help
.PHONY: help fmt fmt-check validate tflint bootstrap plan apply destroy teardown

help: ## Lista los targets disponibles
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

fmt: ## Formatea recursivamente todo el HCL
	terraform fmt -recursive .

fmt-check: ## [CI] Verifica formato sin modificar (falla si hay que formatear)
	terraform fmt -check -recursive .

validate: ## [CI] init -backend=false + validate de módulo y raíz (sin credenciales)
	terraform -chdir=$(TF_MODULE) init -backend=false -input=false
	terraform -chdir=$(TF_MODULE) validate
	terraform -chdir=$(TF_PROD) init -backend=false -input=false
	terraform -chdir=$(TF_PROD) validate

tflint: ## [CI] Linter tflint sobre módulo y raíz
	tflint --init
	tflint --chdir=$(TF_MODULE)
	tflint --chdir=$(TF_PROD)

bootstrap: ## [RUNNER] Bootstrap en dos fases del backend de estado (idempotente)
	./bootstrap.sh

plan: ## [RUNNER] terraform plan de prod (INSPECCIONAR que sea aditivo antes de apply, F1)
	terraform -chdir=$(TF_PROD) plan

apply: ## [RUNNER] terraform apply de prod (sólo tras inspeccionar el plan aditivo, F1)
	terraform -chdir=$(TF_PROD) apply -auto-approve

destroy: teardown ## [RUNNER] Alias de teardown

teardown: ## [RUNNER] Desmonta el backend (¡CUIDADO! ver README; sólo al desmontar toda la pila)
	terraform -chdir=$(TF_PROD) destroy
