# Contribuir a `cam-counter` — política de PRs apilados

> Placeholder de docs de producto; resume la política de entrega. (Ver también el
> `CONTRIBUTING.md` raíz y `CLAUDE.md`.)

La iniciativa se entrega como una **pila de PRs apilados** (*stacked PRs*): cada PR
**ramifica sobre la rama del PR anterior** (PR00 ramifica sobre `main`), y se abre con
`base` = rama del PR anterior, de modo que el diff sea limpio y revisable **de abajo hacia
arriba**.

## Política de merge OBLIGATORIA: **NUNCA `squash`**
- Se permite **merge-commit** o **rebase-merge**; **PROHIBIDO `squash`**.
- **Por qué**: `squash` reescribe la historia de la base y **desincroniza** las ramas
  apiladas superiores.

## Rebase de la pila tras cada merge
Tras mergear `PRn` a `main`, para **cada** rama superior `PRn+1, PRn+2, …`:

```bash
git rebase --onto main <base-antigua> <rama-superior>
git push --force-with-lease
```

## Otras reglas
- **Cero secretos en el repo**: ninguna credencial ni clave AWS de larga vida en git.
- **Preservar la historia**: usar `git mv` para mover/renombrar; nunca `filter-repo` ni
  rebase que rompa SHAs ya publicados.
- **PRs de infra**: el `terraform apply` lo ejecuta el **runner MAD** de forma autónoma
  (plan estrictamente aditivo); GitHub Actions CI permanece **plan-only**.
