# Makefile de Terraform — atajos de fmt/validate/bootstrap/plan/apply/destroy.
#
# DOS ACTORES (no confundir):
#   - CI (GitHub Actions): SOLO-PLAN. Ejecuta `fmt-check`, `validate`, `tflint`. SIN
#     credenciales AWS, SIN `plan`, SIN `apply`. (targets: fmt-check, validate, tflint)
#   - RUNNER MAD: aplica de forma AUTÓNOMA con las credenciales de su ENTORNO (jamás
#     commiteadas). Ejecuta `bootstrap`/`plan`/`apply`/`destroy` contra AWS REAL, SIEMPRE
#     tras inspeccionar el plan para garantizar que es estrictamente aditivo (F1).
#
# Uso: `make -C terraform <target>` desde la raíz del repo.

TF        := terraform
MODULE    := modules/state-backend
ENV       := environments/prod
BUCKET    := cam-counter-tfstate-950639281773
LOCK      := cam-counter-tfstate-lock

.PHONY: fmt fmt-check validate tflint bootstrap plan apply destroy teardown verify-backend

# ───────────────────────── CI / estático (sin credenciales) ─────────────────────────

fmt: ## Formatea recursivamente todo terraform/
	$(TF) fmt -recursive .

fmt-check: ## Verifica formato (lo corre el CI)
	$(TF) fmt -check -recursive .

validate: ## init -backend=false + validate del módulo y de la raíz (lo corre el CI)
	$(TF) -chdir=$(MODULE) init -backend=false
	$(TF) -chdir=$(MODULE) validate
	$(TF) -chdir=$(ENV) init -backend=false
	$(TF) -chdir=$(ENV) validate

tflint: ## Linter (lo corre el CI)
	tflint --chdir=$(MODULE)
	tflint --chdir=$(ENV)

# ───────────────────── RUNNER MAD / apply autónomo (AWS REAL) ─────────────────────

bootstrap: ## Bootstrap en dos fases del backend (RUNNER MAD; idempotente). Ver environments/prod/README.md
	@echo ">> Fase 1: estado LOCAL — init + plan (inspección F1) + apply"
	$(TF) -chdir=$(ENV) init
	$(TF) -chdir=$(ENV) plan -no-color | tee /tmp/cc-bootstrap.plan
	@if grep -Eq 'will be destroyed|must be replaced' /tmp/cc-bootstrap.plan; then \
		echo "!! F1 VIOLADO: el plan contiene destroy/replace. ABORTANDO apply."; exit 1; \
	fi
	$(TF) -chdir=$(ENV) apply -auto-approve
	@echo ">> Fase 2: activar backend.tf y migrar el state a S3"
	cp $(ENV)/backend.tf.example $(ENV)/backend.tf
	$(TF) -chdir=$(ENV) init -migrate-state -force-copy
	@echo ">> Verificación de idempotencia (plan -detailed-exitcode debe dar exit 0)"
	$(TF) -chdir=$(ENV) plan -detailed-exitcode

plan: ## plan contra el backend remoto (RUNNER/PR posteriores; NO en CI). Inspeccionar F1 antes de aplicar.
	$(TF) -chdir=$(ENV) plan

apply: ## apply autónomo (RUNNER MAD tras inspeccionar el plan; NO en CI)
	$(TF) -chdir=$(ENV) apply -auto-approve

verify-backend: ## Comprueba que el backend EXISTE de verdad en AWS
	aws s3api head-bucket --bucket $(BUCKET)
	aws dynamodb describe-table --table-name $(LOCK) \
		--query 'Table.{Keys:KeySchema,Billing:BillingModeSummary.BillingMode}'
	aws dynamodb describe-continuous-backups --table-name $(LOCK) \
		--query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' --output text
	aws s3api head-object --bucket $(BUCKET) --key $(ENV)/terraform.tfstate

destroy teardown: ## Desmonta el backend (ver advertencia en environments/prod/README.md)
	$(TF) -chdir=$(ENV) destroy
