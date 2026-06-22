#!/usr/bin/env python3
"""Azure Boards quarterly feature migrator.

This CLI intentionally uses only Python's standard library and shells out to
`az` so it works well from Windows Bash without jq or extra Python packages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_CONFIG_FILE = "azbm.config.json"
DEFAULT_OPEN_STATES_EXCLUDE = ["Closed", "Done", "Removed", "Resolved"]
DEFAULT_FEATURE_FIELDS_TO_COPY = [
    "System.Description",
    "System.Tags",
    "Microsoft.VSTS.Common.Priority",
    "Microsoft.VSTS.Common.ValueArea",
]
HIERARCHY_FORWARD = "System.LinkTypes.Hierarchy-Forward"
QUARTER_RE = re.compile(r"(?P<year>\d{4})[qQ](?P<q>[1-4])")


class UserError(RuntimeError):
    """Error caused by bad input or external command failure."""


@dataclass
class Config:
    organization: str = ""
    project: str = ""
    area_path: str = ""
    iteration_path_template: str = ""
    default_iteration_path: str = ""
    feature_type: str = "Feature"
    story_types: List[str] = field(default_factory=lambda: ["User Story"])
    open_states_exclude: List[str] = field(
        default_factory=lambda: list(DEFAULT_OPEN_STATES_EXCLUDE)
    )
    copy_feature_fields: List[str] = field(
        default_factory=lambda: list(DEFAULT_FEATURE_FIELDS_TO_COPY)
    )
    extra_feature_fields: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> "Config":
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(
            organization=raw.get("organization", ""),
            project=raw.get("project", ""),
            area_path=raw.get("area_path", raw.get("areaPath", "")),
            iteration_path_template=raw.get(
                "iteration_path_template", raw.get("iterationPathTemplate", "")
            ),
            default_iteration_path=raw.get(
                "default_iteration_path", raw.get("defaultIterationPath", "")
            ),
            feature_type=raw.get("feature_type", raw.get("featureType", "Feature")),
            story_types=list(raw.get("story_types", raw.get("storyTypes", ["User Story"]))),
            open_states_exclude=list(
                raw.get(
                    "open_states_exclude",
                    raw.get("openStatesExclude", DEFAULT_OPEN_STATES_EXCLUDE),
                )
            ),
            copy_feature_fields=list(
                raw.get(
                    "copy_feature_fields",
                    raw.get("copyFeatureFields", DEFAULT_FEATURE_FIELDS_TO_COPY),
                )
            ),
            extra_feature_fields=dict(
                raw.get("extra_feature_fields", raw.get("extraFeatureFields", {}))
            ),
        )

    def apply_env(self) -> None:
        self.organization = os.getenv("AZBM_ORGANIZATION", self.organization)
        self.project = os.getenv("AZBM_PROJECT", self.project)
        self.area_path = os.getenv("AZBM_AREA_PATH", self.area_path)
        self.iteration_path_template = os.getenv(
            "AZBM_ITERATION_PATH_TEMPLATE", self.iteration_path_template
        )
        self.default_iteration_path = os.getenv(
            "AZBM_DEFAULT_ITERATION_PATH", self.default_iteration_path
        )

    def apply_args(self, args: argparse.Namespace) -> None:
        if getattr(args, "organization", None):
            self.organization = args.organization
        if getattr(args, "project", None):
            self.project = args.project
        if getattr(args, "area", None):
            self.area_path = args.area
        if getattr(args, "iteration_template", None):
            self.iteration_path_template = args.iteration_template
        if getattr(args, "default_iteration", None):
            self.default_iteration_path = args.default_iteration
        if getattr(args, "feature_type", None):
            self.feature_type = args.feature_type
        if getattr(args, "story_type", None):
            self.story_types = split_csv(args.story_type)

    def require_devops(self) -> None:
        missing = []
        if not self.organization:
            missing.append("organization")
        if not self.project:
            missing.append("project")
        if not self.area_path:
            missing.append("area_path")
        if missing:
            joined = ", ".join(missing)
            raise UserError(
                f"Faltan valores de configuracion: {joined}. "
                f"Ponlos en {DEFAULT_CONFIG_FILE} o pasalos por parametros."
            )


@dataclass
class WorkItem:
    id: int
    title: str
    state: str
    work_item_type: str
    area_path: str = ""
    iteration_path: str = ""
    url: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)
    relations: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_az(cls, raw: Dict[str, Any]) -> "WorkItem":
        fields = raw.get("fields", {}) or {}
        raw_id = raw.get("id") or fields.get("System.Id")
        if raw_id is None:
            raise UserError(f"Respuesta de az sin id de work item: {raw}")
        return cls(
            id=int(raw_id),
            title=str(fields.get("System.Title", raw.get("title", ""))),
            state=str(fields.get("System.State", raw.get("state", ""))),
            work_item_type=str(
                fields.get("System.WorkItemType", raw.get("workItemType", ""))
            ),
            area_path=str(fields.get("System.AreaPath", "")),
            iteration_path=str(fields.get("System.IterationPath", "")),
            url=str(raw.get("url", "")),
            fields=fields,
            relations=list(raw.get("relations", []) or []),
        )

    def is_open(self, closed_states: Iterable[str]) -> bool:
        closed = {s.casefold() for s in closed_states}
        return self.state.casefold() not in closed

    def short(self) -> str:
        return f"#{self.id} [{self.state}] {self.title}"


@dataclass
class FeaturePlan:
    source: WorkItem
    target_title: str
    from_iteration: str
    to_iteration: str
    target: Optional[WorkItem]
    stories_to_move: List[WorkItem]
    skipped_children: List[Tuple[WorkItem, str]]

    @property
    def will_create_target(self) -> bool:
        return self.target is None


class AzCli:
    def __init__(self, az_command: str, organization: str, project: str, pat: str = ""):
        self.az_command = az_command
        self.organization = organization
        self.project = project
        self.env = os.environ.copy()
        if pat:
            self.env["AZURE_DEVOPS_EXT_PAT"] = pat

    def run_json(self, args: Sequence[str], *, project: bool = True) -> Any:
        full_args = self._base_args(args, project=project) + ["--output", "json"]
        proc = subprocess.run(
            full_args,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise UserError(self._format_failure(full_args, proc))
        stdout = proc.stdout.strip()
        if not stdout:
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise UserError(
                f"az devolvio JSON invalido para: {mask_command(full_args)}\n{exc}\n{stdout}"
            ) from exc

    def run_text(self, args: Sequence[str], *, project: bool = True) -> str:
        full_args = self._base_args(args, project=project)
        proc = subprocess.run(
            full_args,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise UserError(self._format_failure(full_args, proc))
        return proc.stdout

    def _base_args(self, args: Sequence[str], *, project: bool) -> List[str]:
        full_args = [self.az_command, *args, "--only-show-errors"]
        if self.organization and "--org" not in full_args and "--organization" not in full_args:
            full_args.extend(["--org", self.organization])
        if project and self.project and "--project" not in full_args and "-p" not in full_args:
            full_args.extend(["--project", self.project])
        return full_args

    @staticmethod
    def _format_failure(cmd: Sequence[str], proc: subprocess.CompletedProcess[str]) -> str:
        stderr = proc.stderr.strip()
        stdout = proc.stdout.strip()
        parts = [
            f"Comando fallido ({proc.returncode}): {mask_command(cmd)}",
        ]
        if stderr:
            parts.append(stderr)
        if stdout:
            parts.append(stdout)
        return "\n".join(parts)


def split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def mask_command(cmd: Sequence[str]) -> str:
    return " ".join(sh_quote(part) for part in cmd)


def sh_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def normalize_quarter(value: str) -> str:
    match = re.fullmatch(r"\s*(?P<year>\d{4})[qQ](?P<q>[1-4])\s*", value or "")
    if not match:
        raise UserError(f"Trimestre invalido: {value!r}. Usa formato 2026q1..2026q4.")
    return f"{match.group('year')}q{match.group('q')}"


def next_quarter(value: str) -> str:
    quarter = normalize_quarter(value)
    year = int(quarter[:4])
    q = int(quarter[-1])
    if q == 4:
        return f"{year + 1}q1"
    return f"{year}q{q + 1}"


def render_iteration_path(template: str, quarter: str) -> str:
    if not template:
        raise UserError(
            "No hay iteration path. Pasa --iteration/--from-iteration/--to-iteration "
            "o configura iteration_path_template."
        )
    q_norm = normalize_quarter(quarter)
    year = q_norm[:4]
    qnum = q_norm[-1]
    return template.format(
        quarter=q_norm,
        quarter_upper=q_norm.upper(),
        year=year,
        q=f"q{qnum}",
        Q=f"Q{qnum}",
        qnum=qnum,
    )


def replace_quarter_in_title(title: str, from_q: str, to_q: str) -> str:
    from_norm = normalize_quarter(from_q)
    to_norm = normalize_quarter(to_q)
    pattern = re.compile(re.escape(from_norm), re.IGNORECASE)
    new_title, count = pattern.subn(to_norm, title, count=1)
    if count != 1:
        raise UserError(f"El titulo no contiene {from_norm}: {title}")
    return new_title


def title_matches_prefix_quarter(
    title: str, prefix: Optional[str], quarter: Optional[str]
) -> bool:
    if quarter:
        q_norm = normalize_quarter(quarter)
        match = re.search(re.escape(q_norm), title, flags=re.IGNORECASE)
        if not match:
            return False
    else:
        match = None

    if not prefix:
        return True

    if match is None:
        normalized_title = normalize_prefix_segment(title)
    else:
        normalized_title = normalize_prefix_segment(title[: match.start()])
    return normalized_title.casefold() == normalize_prefix_segment(prefix).casefold()


def normalize_prefix_segment(value: str) -> str:
    return re.sub(r"[\s\-_:/#]+$", "", value.strip())


def wiql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_open_features_wiql(
    config: Config,
    iteration_path: str,
    title_contains: str = "",
    exact_title: str = "",
    include_closed: bool = False,
) -> str:
    conditions = [
        f"[System.TeamProject] = {wiql_quote(config.project)}",
        f"[System.WorkItemType] = {wiql_quote(config.feature_type)}",
        f"[System.AreaPath] = {wiql_quote(config.area_path)}",
        f"[System.IterationPath] = {wiql_quote(iteration_path)}",
    ]
    if exact_title:
        conditions.append(f"[System.Title] = {wiql_quote(exact_title)}")
    elif title_contains:
        # Keep quarter/prefix filtering client-side. WIQL text operators differ by
        # field/index support, while exact title matching is stable enough here.
        pass
    if config.open_states_exclude and not include_closed:
        states = ", ".join(wiql_quote(state) for state in config.open_states_exclude)
        conditions.append(f"[System.State] NOT IN ({states})")
    return (
        "SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType] "
        "FROM WorkItems WHERE "
        + " AND ".join(conditions)
        + " ORDER BY [System.Title]"
    )


def query_features(
    az: AzCli,
    config: Config,
    iteration_path: str,
    *,
    title_contains: str = "",
    exact_title: str = "",
    include_closed: bool = False,
) -> List[WorkItem]:
    wiql = build_open_features_wiql(
        config,
        iteration_path,
        title_contains=title_contains,
        exact_title=exact_title,
        include_closed=include_closed,
    )
    raw = az.run_json(["boards", "query", "--wiql", wiql])
    candidates = query_result_items(raw)
    items: List[WorkItem] = []
    for candidate in candidates:
        raw_id = candidate.get("id") or candidate.get("fields", {}).get("System.Id")
        if raw_id is None:
            continue
        items.append(show_work_item(az, int(raw_id), expand_relations=True))
    return items


def query_result_items(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("workItems", "value", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "id" in raw or "fields" in raw:
            return [raw]
    raise UserError(f"No entiendo la respuesta de az boards query: {raw}")


def show_work_item(az: AzCli, item_id: int, *, expand_relations: bool = False) -> WorkItem:
    args = ["boards", "work-item", "show", "--id", str(item_id)]
    if expand_relations:
        args.extend(["--expand", "relations"])
    raw = az.run_json(args, project=False)
    if not isinstance(raw, dict):
        raise UserError(f"No entiendo la respuesta de work-item show para {item_id}: {raw}")
    return WorkItem.from_az(raw)


def relation_target_id(relation: Dict[str, Any]) -> Optional[int]:
    url = str(relation.get("url", ""))
    match = re.search(r"/workItems/(\d+)(?:\?.*)?$", url)
    if not match:
        match = re.search(r"/workitems/(\d+)(?:\?.*)?$", url, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def child_work_item_ids(feature: WorkItem) -> List[int]:
    ids: List[int] = []
    for relation in feature.relations:
        if relation.get("rel") != HIERARCHY_FORWARD:
            continue
        target_id = relation_target_id(relation)
        if target_id is not None:
            ids.append(target_id)
    return ids


def load_config(args: argparse.Namespace) -> Config:
    config_path = resolve_config_path(getattr(args, "config", DEFAULT_CONFIG_FILE))
    config = Config.from_file(config_path)
    config.apply_env()
    config.apply_args(args)
    return config


def resolve_config_path(value: str) -> Path:
    config_path = Path(value)
    if config_path.is_absolute() or config_path.exists():
        return config_path
    script_dir_candidate = Path(__file__).resolve().parent / value
    if script_dir_candidate.exists():
        return script_dir_candidate
    return config_path


def resolve_pat(args: argparse.Namespace) -> str:
    if getattr(args, "pat", ""):
        return args.pat
    env_name = getattr(args, "pat_env", "AZURE_DEVOPS_EXT_PAT")
    return os.getenv(env_name, "")


def make_az(args: argparse.Namespace, config: Config) -> AzCli:
    return AzCli(
        az_command=getattr(args, "az_command", "az"),
        organization=config.organization,
        project=config.project,
        pat=resolve_pat(args),
    )


def resolve_iteration_for_list(args: argparse.Namespace, config: Config) -> str:
    if getattr(args, "iteration", ""):
        return args.iteration
    if getattr(args, "quarter", ""):
        return render_iteration_path(config.iteration_path_template, args.quarter)
    if config.default_iteration_path:
        return config.default_iteration_path
    raise UserError(
        "Indica --iteration, --quarter con iteration_path_template, "
        "o default_iteration_path en la configuracion."
    )


def resolve_migration_iterations(args: argparse.Namespace, config: Config) -> Tuple[str, str]:
    from_q = normalize_quarter(args.from_q)
    to_q = normalize_quarter(args.to_q or next_quarter(from_q))

    if args.from_iteration:
        from_iteration = args.from_iteration
    else:
        from_iteration = render_iteration_path(config.iteration_path_template, from_q)

    if args.to_iteration:
        to_iteration = args.to_iteration
    else:
        to_iteration = render_iteration_path(config.iteration_path_template, to_q)

    return from_iteration, to_iteration


def find_open_features(
    az: AzCli,
    config: Config,
    iteration_path: str,
    *,
    prefix: str = "",
    quarter: str = "",
) -> List[WorkItem]:
    title_contains = normalize_quarter(quarter) if quarter else ""
    features = query_features(
        az,
        config,
        iteration_path,
        title_contains=title_contains,
        include_closed=False,
    )
    return [
        item
        for item in features
        if title_matches_prefix_quarter(item.title, prefix or None, quarter or None)
    ]


def build_feature_plan(
    az: AzCli,
    config: Config,
    source: WorkItem,
    *,
    from_q: str,
    to_q: str,
    from_iteration: str,
    to_iteration: str,
    include_closed_stories: bool,
) -> FeaturePlan:
    target_title = replace_quarter_in_title(source.title, from_q, to_q)
    targets = query_features(
        az,
        config,
        to_iteration,
        exact_title=target_title,
        include_closed=True,
    )
    target = targets[0] if targets else None

    stories_to_move: List[WorkItem] = []
    skipped_children: List[Tuple[WorkItem, str]] = []
    allowed_types = {item.casefold() for item in config.story_types}

    for child_id in child_work_item_ids(source):
        child = show_work_item(az, child_id, expand_relations=False)
        if child.work_item_type.casefold() not in allowed_types:
            skipped_children.append(
                (child, f"tipo {child.work_item_type!r} no esta en {config.story_types}")
            )
            continue
        if not include_closed_stories and not child.is_open(config.open_states_exclude):
            skipped_children.append((child, f"estado cerrado/ignorado {child.state!r}"))
            continue
        stories_to_move.append(child)

    return FeaturePlan(
        source=source,
        target_title=target_title,
        from_iteration=from_iteration,
        to_iteration=to_iteration,
        target=target,
        stories_to_move=stories_to_move,
        skipped_children=skipped_children,
    )


def create_feature(az: AzCli, config: Config, source: WorkItem, target_title: str, to_iteration: str) -> WorkItem:
    fields = dict(config.extra_feature_fields)
    for field_name in config.copy_feature_fields:
        if field_name in source.fields and source.fields[field_name] not in (None, ""):
            fields.setdefault(field_name, source.fields[field_name])

    args = [
        "boards",
        "work-item",
        "create",
        "--type",
        config.feature_type,
        "--title",
        target_title,
        "--area",
        config.area_path,
        "--iteration",
        to_iteration,
    ]
    description = fields.pop("System.Description", None)
    if description:
        args.extend(["--description", str(description)])
    if fields:
        args.append("--fields")
        args.extend(f"{key}={value}" for key, value in fields.items())

    raw = az.run_json(args)
    if not isinstance(raw, dict):
        raise UserError(f"No entiendo la respuesta al crear feature: {raw}")
    return WorkItem.from_az(raw)


def move_story_to_feature(
    az: AzCli,
    story: WorkItem,
    old_feature: WorkItem,
    new_feature: WorkItem,
    to_iteration: str,
) -> None:
    # Azure Boards normally allows one parent. Remove the old child relation first,
    # then create the new child relation and move the story to the target iteration.
    az.run_json(
        [
            "boards",
            "work-item",
            "relation",
            "remove",
            "--id",
            str(old_feature.id),
            "--relation-type",
            "child",
            "--target-id",
            str(story.id),
            "--yes",
        ],
        project=False,
    )
    az.run_json(
        [
            "boards",
            "work-item",
            "relation",
            "add",
            "--id",
            str(new_feature.id),
            "--relation-type",
            "child",
            "--target-id",
            str(story.id),
        ],
        project=False,
    )
    az.run_json(
        [
            "boards",
            "work-item",
            "update",
            "--id",
            str(story.id),
            "--iteration",
            to_iteration,
        ],
        project=False,
    )


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        print("(sin resultados)")
        return
    widths = [
        max(len(str(header)), *(len(str(row[index])) for row in rows))
        for index, header in enumerate(headers)
    ]
    print("  ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)))


def command_next_q(args: argparse.Namespace) -> int:
    print(next_quarter(args.quarter))
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    az_command = getattr(args, "az_command", "az")
    az_path = shutil.which(az_command)
    if not az_path:
        print(f"ERROR: no encuentro {az_command!r} en PATH.")
        print("Instala Azure CLI y vuelve a ejecutar este comando.")
        return 2

    print(f"az: {az_path}")
    version = subprocess.run(
        [az_command, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    first_lines = "\n".join(version.stdout.splitlines()[:6])
    if first_lines:
        print(first_lines)

    extension = subprocess.run(
        [az_command, "extension", "show", "--name", "azure-devops", "--output", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if extension.returncode == 0:
        data = json.loads(extension.stdout)
        print(f"azure-devops extension: {data.get('version', '(version desconocida)')}")
    else:
        print("ERROR: falta la extension azure-devops.")
        print("Instalala con: az extension add --name azure-devops")
        return 2
    return 0


def command_list_open(args: argparse.Namespace) -> int:
    config = load_config(args)
    config.require_devops()
    iteration_path = resolve_iteration_for_list(args, config)
    az = make_az(args, config)

    quarter = getattr(args, "quarter", "") or ""
    features = find_open_features(
        az,
        config,
        iteration_path,
        prefix=getattr(args, "prefix", "") or "",
        quarter=quarter,
    )

    if args.json:
        print(json.dumps([item_to_json(item) for item in features], indent=2, ensure_ascii=False))
        return 0

    print(f"Features abiertas en AreaPath={config.area_path!r}, IterationPath={iteration_path!r}")
    rows = [
        [item.id, item.state, item.work_item_type, item.title]
        for item in sorted(features, key=lambda item: item.title.casefold())
    ]
    print_table(["ID", "Estado", "Tipo", "Titulo"], rows)
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    config = load_config(args)
    config.require_devops()
    if args.json and args.apply:
        raise UserError("--json muestra el plan y no se combina con --apply.")

    from_q = normalize_quarter(args.from_q)
    to_q = normalize_quarter(args.to_q or next_quarter(from_q))
    from_iteration, to_iteration = resolve_migration_iterations(args, config)
    az = make_az(args, config)

    sources = find_open_features(
        az,
        config,
        from_iteration,
        prefix=args.prefix,
        quarter=from_q,
    )
    if not sources:
        print(
            "No hay features abiertas que coincidan con "
            f"prefix={args.prefix!r}, quarter={from_q!r}, iteration={from_iteration!r}."
        )
        return 1

    plans = [
        build_feature_plan(
            az,
            config,
            source,
            from_q=from_q,
            to_q=to_q,
            from_iteration=from_iteration,
            to_iteration=to_iteration,
            include_closed_stories=args.include_closed_stories,
        )
        for source in sources
    ]

    if args.json:
        print(json.dumps([plan_to_json(plan) for plan in plans], indent=2, ensure_ascii=False))
        return 0

    print_migration_plan(plans, apply=args.apply)
    if not args.apply:
        print("\nDRY-RUN: no se ha cambiado Azure Boards. Pasa --apply para ejecutar.")
        return 0

    created = 0
    moved = 0
    for plan in plans:
        target = plan.target
        if target is None:
            print(f"\nCreando Feature destino para {plan.source.short()}: {plan.target_title}")
            target = create_feature(az, config, plan.source, plan.target_title, plan.to_iteration)
            created += 1
            print(f"Creada {target.short()}")
        else:
            print(f"\nUsando Feature destino existente: {target.short()}")

        for story in plan.stories_to_move:
            print(f"Moviendo User Story {story.short()} -> Feature #{target.id}")
            move_story_to_feature(az, story, plan.source, target, plan.to_iteration)
            moved += 1

    print(f"\nHecho. Features creadas: {created}. User Stories migradas: {moved}.")
    return 0


def print_migration_plan(plans: Sequence[FeaturePlan], *, apply: bool) -> None:
    print("Plan de migracion" + (" (APPLY)" if apply else ""))
    total_create = 0
    total_reuse = 0
    total_stories = 0
    for plan in plans:
        print("")
        print(f"Origen:  {plan.source.short()}")
        print(f"Desde:   {plan.from_iteration}")
        if plan.target:
            total_reuse += 1
            print(f"Destino: reutilizar {plan.target.short()}")
        else:
            total_create += 1
            print(f"Destino: crear Feature {plan.target_title!r}")
        print(f"Hacia:   {plan.to_iteration}")

        if plan.stories_to_move:
            print("User Stories a migrar:")
            for story in plan.stories_to_move:
                total_stories += 1
                print(f"  - {story.short()} ({story.iteration_path})")
        else:
            print("User Stories a migrar: ninguna")

        if plan.skipped_children:
            print("Hijos ignorados:")
            for child, reason in plan.skipped_children:
                print(f"  - {child.short()} ({reason})")

    print("")
    print(
        "Resumen: "
        f"features a crear={total_create}, "
        f"features destino existentes={total_reuse}, "
        f"user stories a migrar={total_stories}"
    )


def item_to_json(item: WorkItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "state": item.state,
        "type": item.work_item_type,
        "areaPath": item.area_path,
        "iterationPath": item.iteration_path,
        "url": item.url,
    }


def plan_to_json(plan: FeaturePlan) -> Dict[str, Any]:
    return {
        "source": item_to_json(plan.source),
        "targetTitle": plan.target_title,
        "fromIteration": plan.from_iteration,
        "toIteration": plan.to_iteration,
        "target": item_to_json(plan.target) if plan.target else None,
        "willCreateTarget": plan.will_create_target,
        "storiesToMove": [item_to_json(item) for item in plan.stories_to_move],
        "skippedChildren": [
            {"item": item_to_json(item), "reason": reason}
            for item, reason in plan.skipped_children
        ],
    }


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="JSON de configuracion.")
    parser.add_argument("--organization", "--org", help="URL de Azure DevOps.")
    parser.add_argument("--project", help="Proyecto de Azure DevOps.")
    parser.add_argument("--area", help="System.AreaPath fijo.")
    parser.add_argument("--iteration-template", help="Plantilla de iteracion con {quarter}.")
    parser.add_argument("--default-iteration", help="IterationPath fijo para list-open.")
    parser.add_argument("--feature-type", help="Tipo de work item feature.")
    parser.add_argument("--story-type", help="Tipos historia separados por coma.")
    parser.add_argument("--pat", help="PAT. Mejor usar variable de entorno si puedes.")
    parser.add_argument(
        "--pat-env",
        default="AZURE_DEVOPS_EXT_PAT",
        help="Variable de entorno que contiene el PAT.",
    )
    parser.add_argument("--az-command", default="az", help="Binario az a ejecutar.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="azbm",
        description="Migra Features trimestrales de Azure Boards usando Azure CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Comprueba Azure CLI y extension.")
    doctor.add_argument("--az-command", default="az", help="Binario az a ejecutar.")
    doctor.set_defaults(func=command_doctor)

    next_q = subparsers.add_parser("next-q", help="Calcula el siguiente trimestre.")
    next_q.add_argument("quarter", help="Ejemplo: 2026q4")
    next_q.set_defaults(func=command_next_q)

    list_open = subparsers.add_parser("list-open", help="Lista Features abiertas.")
    add_common_args(list_open)
    list_open.add_argument("--quarter", help="Filtra por trimestre y resuelve iteracion.")
    list_open.add_argument("--iteration", help="IterationPath exacto.")
    list_open.add_argument("--prefix", help="Prefijo/titulo antes del trimestre.")
    list_open.add_argument("--json", action="store_true", help="Salida JSON.")
    list_open.set_defaults(func=command_list_open)

    migrate = subparsers.add_parser("migrate", help="Planifica o ejecuta migracion.")
    add_common_args(migrate)
    migrate.add_argument("--prefix", required=True, help="Prefijo a migrar, ejemplo: app1.")
    migrate.add_argument("--from-q", required=True, help="Trimestre origen, ejemplo: 2026q1.")
    migrate.add_argument("--to-q", help="Trimestre destino. Por defecto, siguiente.")
    migrate.add_argument("--from-iteration", help="IterationPath origen exacto.")
    migrate.add_argument("--to-iteration", help="IterationPath destino exacto.")
    migrate.add_argument(
        "--include-closed-stories",
        action="store_true",
        help="Tambien migra historias con estados cerrados/ignorados.",
    )
    migrate.add_argument("--json", action="store_true", help="Salida JSON del plan.")
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="Ejecuta cambios reales. Sin esto, migrate es dry-run.",
    )
    migrate.set_defaults(func=command_migrate)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except UserError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrumpido.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
