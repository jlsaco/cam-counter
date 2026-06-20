# Contribuir a `cam-counter` — Política de PRs apilados (placeholder)

> Placeholder; se amplía en PRs posteriores. Resume la política de entrega del producto.

La iniciativa se entrega como una **pila de PRs apilados** (*stacked PRs*):

- Cada PR **ramifica sobre la rama del PR anterior** (PR00 ramifica sobre `main`), y se
  abre con `base = rama del PR anterior` para que el diff sea limpio y revisable **de abajo
  hacia arriba**.
- Un humano revisa y mergea **de abajo hacia arriba**.

## Política de merge OBLIGATORIA: **NUNCA `squash`**

- Se permite **merge-commit** o **rebase-merge**; **PROHIBIDO `squash`**.
- **Por qué**: `squash` reescribe la historia de la base y **desincroniza** las ramas
  apiladas superiores.

## Procedimiento de rebase de la pila tras cada merge

Tras mergear `PRn` a `main`, para **cada** rama superior `PRn+1, PRn+2, …`:

```bash
git rebase --onto main <base-antigua> <rama-superior>
git push --force-with-lease
```

## Notas

- **Cero secretos en git**: nada de claves de larga vida ni contraseñas de cámara en claro;
  credenciales por env/SSM/Secrets Manager. `gitleaks` corre en CI.
- **PRs de infra**: el runner MAD ejecuta `terraform apply` de forma autónoma desde la rama
  apilada más alta; GitHub Actions CI permanece **plan-only**. Ver `CLAUDE.md` (F1/F2/F3).
