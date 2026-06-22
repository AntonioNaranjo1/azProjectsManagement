# Azure Backlog Migrator

CLI para Windows Bash que usa comandos `az` directamente para analizar Features abiertas de Azure Boards y migrarlas entre trimestres con patron `YYYYqN`, por ejemplo `app1 2026q1 -> app1 2026q2` o `app32026q4 -> app32027q1`.

Por seguridad, `migrate` es `dry-run` por defecto. Solo modifica Azure Boards cuando se pasa `--apply`.

## Requisitos

- Windows Bash: Git Bash, MSYS2 o similar.
- Azure CLI.
- Extension Azure DevOps.
- `jq`, preferiblemente en `bin/jq.exe` dentro del repo en Windows.

```bash
az extension add --name azure-devops
```

El script busca `jq` en este orden: `bin/jq.exe`, `bin/jq`, y despues `jq` en el `PATH`.

## Configuracion

Crea `azbm.config.json` a partir de `config.example.json` y ajusta:

```json
{
  "organization": "https://dev.azure.com/mi-org",
  "project": "MiProyecto",
  "area_path": "MiProyecto\\MiArea",
  "iteration_path_template": "MiProyecto\\{year}\\{quarter_upper}",
  "feature_type": "Feature",
  "story_types": ["User Story"],
  "feature_active_state": "Active",
  "feature_resolved_state": "Resolved",
  "feature_closed_state": "Closed",
  "feature_start_date_field": "Microsoft.VSTS.Scheduling.StartDate",
  "feature_end_date_field": "Microsoft.VSTS.Scheduling.TargetDate"
}
```

La plantilla `iteration_path_template` puede usar:

- `{quarter}`: `2026q1`
- `{quarter_upper}`: `2026Q1`
- `{year}`: `2026`
- `{q}`: `q1`
- `{Q}`: `Q1`
- `{qnum}`: `1`

Los trimestres no distinguen mayusculas/minusculas: `2026q1` y `2026Q1` se tratan igual. Al crear el titulo destino, el script conserva el estilo del titulo origen: `app1 2026Q1` pasa a `app1 2026Q2`, y `app1 2026q1` pasa a `app1 2026q2`.

Si tu iteracion no sigue una plantilla, pasa `--from-iteration` y `--to-iteration` al migrar.

`iteration_path_template` se usa para calcular un `IterationPath` a partir del trimestre que pasas al comando. Por ejemplo, con `MiProyecto\\{year}\\{quarter_upper}`, `--quarter 2026q1` se convierte en `MiProyecto\\2026\\2026Q1`.

`default_iteration_path` solo se usa en `list-open` cuando no pasas ni `--quarter` ni `--iteration`. Es un valor fijo de respaldo; no sirve para calcular el trimestre siguiente en `migrate`.

## Autenticacion

Opcion recomendada en Bash:

```bash
export AZURE_DEVOPS_EXT_PAT="tu_pat"
```

Tambien puedes pasarlo al comando:

```bash
./azbm.sh list-open --pat "tu_pat" --quarter 2026q1
```

El PAT debe tener permisos suficientes para leer y modificar Work Items.

## Comandos

Comprobar instalacion:

```bash
./azbm.sh doctor
```

Ver el siguiente trimestre:

```bash
./azbm.sh next-q 2026q4
# 2027q1
```

Listar Features abiertas de un trimestre:

```bash
./azbm.sh list-open --quarter 2026q1
```

Filtrar por prefijo:

```bash
./azbm.sh list-open --quarter 2026q1 --prefix app1
```

Planificar migracion, sin escribir nada:

```bash
./azbm.sh migrate --prefix app1 --from-q 2026q1
```

Ejecutar migracion real:

```bash
./azbm.sh migrate --prefix app1 --from-q 2026q1 --apply
```

Indicar destino explicitamente:

```bash
./azbm.sh migrate --prefix app3 --from-q 2026q4 --to-q 2027q1 --apply
```

Si no pasas `--to-q`, el destino es el siguiente trimestre.

## Que hace la migracion

Para cada Feature abierta que coincida con `--prefix` y `--from-q`:

1. Calcula el titulo destino reemplazando el trimestre en el titulo.
2. Busca si ya existe una Feature destino con ese titulo en el `IterationPath` destino.
3. Si no existe, crea una nueva Feature con el mismo titulo migrado, misma AreaPath y campos configurados en `copy_feature_fields`.
4. Mantiene la misma epica padre de la Feature origen enlazando la Feature destino bajo esa epica.
5. Deja la Feature destino en `feature_active_state`.
6. Pone la fecha de inicio y fin de la Feature destino al primer y ultimo dia del quarter destino.
7. Detecta hijos de tipo `User Story` por relacion padre-hijo.
8. Mueve esas historias desde la Feature origen a la Feature destino.
9. Actualiza el `IterationPath` de cada historia al destino.
10. Cierra la Feature origen pasando antes por `feature_resolved_state` y despues por `feature_closed_state`.

Por defecto las fechas se escriben en:

- `Microsoft.VSTS.Scheduling.StartDate`
- `Microsoft.VSTS.Scheduling.TargetDate`

Si tu proceso usa estados en espanol o personalizados, cambia estos valores en `azbm.config.json`:

```json
{
  "feature_active_state": "Active",
  "feature_resolved_state": "Resolved",
  "feature_closed_state": "Closed"
}
```

Si la Feature destino ya existe y cuelga de otra epica, `--apply` se detiene para no mezclar jerarquias.

Los hijos con tipo distinto a `story_types` o estados en `open_states_exclude` se muestran como ignorados. Para incluir historias cerradas:

```bash
./azbm.sh migrate --prefix app1 --from-q 2026q1 --include-closed-stories
```

## Notas de seguridad

Pasar un PAT por argumento puede dejarlo visible en el historial de shell o en la lista de procesos. Para uso diario, usa `AZURE_DEVOPS_EXT_PAT`.

Ejecuta primero siempre sin `--apply`. El modo dry-run muestra que Feature crearia o reutilizaria y que User Stories moveria.

## Referencias oficiales

- Azure DevOps CLI: https://learn.microsoft.com/en-us/azure/devops/cli/?view=azure-devops
- Login con PAT / `AZURE_DEVOPS_EXT_PAT`: https://learn.microsoft.com/en-us/azure/devops/cli/log-in-via-pat?view=azure-devops
- `az boards query`: https://learn.microsoft.com/en-us/cli/azure/boards?view=azure-cli-latest
- `az boards work-item`: https://learn.microsoft.com/en-us/cli/azure/boards/work-item?view=azure-cli-latest
- Relaciones de work items: https://learn.microsoft.com/en-us/cli/azure/boards/work-item/relation?view=azure-cli-latest
