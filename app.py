"""Flask accounting page for overlapping building insulation reports."""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import re
from typing import Any

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import storage
from storage import Database, StorageError, BuildingInput


def create_app(database_path: str | Path | Database | None = None) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    if isinstance(database_path, Database):
        db = database_path
    else:
        path = (
            database_path
            or os.environ.get("ACCOUNTING_DB")
            or os.environ.get("DATABASE_PATH")
            or Path(app.instance_path) / "accounting.db"
        )
        db = Database(path)
    app.config["ACCOUNTING_DATABASE"] = db

    def get_db() -> Any:
        if "db" not in g:
            g.db = db.connect()
        return g.db

    @app.teardown_appcontext
    def close_db(_exception: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            db.release(conn)

    def render_page(
        *,
        status_code: int = 200,
        error: str | None = None,
        success: str | None = None,
        report_form: dict[str, Any] | None = None,
        building_text: str | None = None,
        variance_values: list[str] | None = None,
        edit_id: str | None = None,
    ):
        state = storage.get_state(get_db())
        form_values = {
            "report_id": "",
            "mean": "",
            "buildings": "",
            "selected": [],
        }
        if report_form is not None:
            form_values.update(report_form)
        elif edit_id:
            for report in state["reports"]:
                if report["report_id"] == edit_id:
                    form_values = {
                        "report_id": report["report_id"],
                        "mean": report["mean_text"],
                        "buildings": ", ".join(report["building_codes"]),
                        "selected": report["building_nos"],
                    }
                    break

        if building_text is None:
            # Put each existing variance on its own line. The textarea parser
            # also accepts comma/whitespace-separated input.
            building_text = "\n".join(row["variance_text"] for row in state["buildings"])

        if variance_values is None:
            variance_values = [row["variance_text"] for row in state["buildings"]]
        padded_variance_values = (list(variance_values) + [""] * storage.MAX_BUILDINGS)[
            : storage.MAX_BUILDINGS
        ]

        html = render_template(
            "index.html",
            state=state,
            computation=state["computation"],
            error=error,
            success=success,
            report_form=form_values,
            building_text=building_text,
            variance_values=padded_variance_values,
            max_buildings=storage.MAX_BUILDINGS,
            max_reports=storage.MAX_REPORTS,
        )
        return html, status_code

    def exact_json() -> Any:
        body = request.get_data(as_text=True)
        if not body.strip():
            return {}
        try:
            return json.loads(body, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise StorageError(f"JSON 格式不正确：{exc.msg}") from exc

    def split_coverage(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            tokens: list[Any] = []
            for item in value:
                tokens.extend(split_coverage(item))
            return tokens
        if isinstance(value, int) and not isinstance(value, bool):
            return [value]
        text = str(value).strip()
        if not text:
            return []
        return [
            token
            for token in re.split(r"[,，;；、\s]+", text)
            if token
        ]

    def building_inputs_from_form() -> tuple[list[BuildingInput], str]:
        # Prefer repeated ordered variance fields (one input per building).
        raw_values = request.form.getlist("variance")
        if not any(str(value).strip() for value in raw_values):
            raw_values = request.form.getlist("building_variance")

        if any(str(value).strip() for value in raw_values):
            source_values = raw_values
        else:
            # Also support one-per-line/comma/space-separated bulk entry.
            textarea = (
                request.form.get("variances_bulk")
                or request.form.get("variances_text")
                or request.form.get("variances")
                or ""
            )
            source_values = [
                token
                for token in re.split(r"[,，;；、\s]+", textarea)
                if token.strip()
            ]

        if not source_values or not any(str(value).strip() for value in source_values):
            raise storage.ValidationError("请先录入至少一栋楼的正方差。")

        # The rendered page has 20 ordered inputs. Ignore only trailing blanks;
        # an interior blank means an explicitly numbered building is missing.
        last_nonempty = -1
        for index, value in enumerate(source_values):
            if str(value).strip():
                last_nonempty = index
        if last_nonempty < 0:
            raise storage.ValidationError("请先录入至少一栋楼的正方差。")

        items: list[BuildingInput] = []
        for index in range(last_nonempty + 1):
            raw = source_values[index] if index < len(source_values) else ""
            text = str(raw).strip()
            if not text:
                raise storage.ValidationError(
                    f"第 {index + 1} 栋楼的方差为空；请补全或删除后面的楼。"
                )
            items.append(BuildingInput(variance=text, variance_text=text))

        text_for_page = "\n".join(
            str(value).strip() for value in source_values[: last_nonempty + 1]
        )
        return items, text_for_page

    def building_inputs_from_json(data: Any) -> list[BuildingInput]:
        if isinstance(data, list):
            raw_entries = data
        elif isinstance(data, dict):
            raw_entries = None
            for key in (
                "buildings",
                "items",
                "building_variances",
                "variances",
                "variance_values",
            ):
                if key in data:
                    raw_entries = data[key]
                    break
            if raw_entries is None:
                # Allow {"1": 1, "2": 2} as a building-number -> variance map.
                numeric_keys = [key for key in data if str(key).strip().isdigit()]
                if numeric_keys:
                    raw_entries = [data[key] for key in sorted(numeric_keys, key=int)]
                else:
                    raise storage.ValidationError(
                        "请提供 variances 列表或 buildings 列表。"
                    )
        else:
            raise storage.ValidationError("楼宇方差数据格式不正确。")

        if not isinstance(raw_entries, list):
            # {"variances": "1,2,3"} convenience.
            raw_entries = split_coverage(raw_entries)

        items: list[BuildingInput] = []
        for index, entry in enumerate(raw_entries, start=1):
            if isinstance(entry, dict):
                variance = None
                for key in (
                    "variance",
                    "variance_text",
                    "variance_value",
                    "var",
                    "sigma_squared",
                    "value",
                ):
                    if key in entry:
                        variance = entry[key]
                        break
                if variance is None:
                    raise storage.ValidationError(
                        f"第 {index} 栋楼缺少 variance 正方差字段。"
                    )
                code = entry.get("code")
                if code is None and isinstance(entry.get("building_id"), str):
                    code = entry["building_id"]
                if code is None and isinstance(entry.get("id"), str):
                    code = entry["id"]
                label = entry.get("label", "")
                raw_text = str(variance)
                items.append(
                    BuildingInput(
                        variance=variance,
                        variance_text=raw_text,
                        code=str(code) if code is not None else None,
                        label=str(label),
                    )
                )
            else:
                items.append(
                    BuildingInput(variance=entry, variance_text=str(entry).strip())
                )
        return items

    def report_fields_from_form(url_report_id: str | None) -> dict[str, Any]:
        report_id = (
            url_report_id
            or request.form.get("report_id")
            or request.form.get("reportId")
            or request.form.get("report-id")
            or request.form.get("id")
            or request.form.get("report_no")
            or request.form.get("reportNo")
            or request.form.get("number")
            or ""
        )
        mean_raw = (
            request.form.get("mean")
            or request.form.get("mean_value")
            or request.form.get("mean-value")
            or request.form.get("meanValue")
            or request.form.get("average")
            or request.form.get("average_value")
            or request.form.get("equal_weight_mean")
            or request.form.get("value")
            or ""
        )
        coverage: list[Any] = []
        explicit_text = request.form.get("buildings", "")
        if explicit_text.strip():
            coverage.extend(split_coverage(explicit_text))
        else:
            for key in (
                "building_ids",
                "buildingIds",
                "building-ids",
                "building_nos",
                "buildingNos",
            ):
                coverage.extend(request.form.getlist(key))
            for key in (
                "covered_buildings",
                "coveredBuildingIds",
                "coverage",
                "building_id",
            ):
                for value in request.form.getlist(key):
                    coverage.extend(split_coverage(value))
            if not coverage:
                coverage.extend(split_coverage(request.form.get("buildings_text", "")))

        selected: list[int] = []
        for key in ("building_ids", "buildingIds", "building_nos"):
            for value in request.form.getlist(key):
                try:
                    selected.append(int(value))
                except (TypeError, ValueError):
                    pass

        mean_text = str(mean_raw).strip()
        return {
            "report_id": str(report_id).strip(),
            "mean": mean_raw,
            "mean_text": mean_text,
            "building_tokens": coverage,
            "form": {
                "report_id": str(report_id).strip(),
                "mean": mean_text,
                "buildings": ", ".join(str(v) for v in split_coverage(coverage)),
                "selected": selected,
            },
        }

    def report_fields_from_json(data: Any, url_report_id: str | None) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise storage.ValidationError("报告 JSON 必须是对象。")
        report_id = url_report_id
        if not report_id:
            for key in (
                "report_id",
                "reportId",
                "id",
                "report_no",
                "reportNo",
                "number",
            ):
                if data.get(key) is not None:
                    report_id = str(data[key])
                    break
        mean_raw = None
        for key in (
            "mean",
            "mean_text",
            "meanValue",
            "mean_value",
            "equal_weight_mean",
            "equalWeightMean",
            "average",
            "average_value",
            "avg",
            "mean_value",
            "value",
        ):
            if key in data:
                mean_raw = data[key]
                break
        if mean_raw is None:
            raise storage.ValidationError("报告缺少 mean 等权均值。")

        coverage_raw = None
        for key in (
            "building_ids",
            "buildingIds",
            "building_nos",
            "buildingNos",
            "buildings",
            "covered_buildings",
            "coveredBuildingIds",
            "coverage",
        ):
            if key in data:
                coverage_raw = data[key]
                break
        tokens = split_coverage(coverage_raw)
        mean_text = mean_raw if isinstance(mean_raw, str) else str(mean_raw)
        return {
            "report_id": (report_id or "").strip(),
            "mean": mean_raw,
            "mean_text": mean_text,
            "building_tokens": tokens,
            "form": {
                "report_id": (report_id or "").strip(),
                "mean": mean_text,
                "buildings": ", ".join(str(v) for v in tokens),
                "selected": [v for v in tokens if isinstance(v, int)],
            },
        }

    def wants_json() -> bool:
        return (
            request.path.startswith("/api/")
            or request.is_json
            or "application/json" in request.headers.get("Accept", "")
        )

    def json_state(state: dict[str, Any], **extra: Any):
        computation = state.get("computation") or {}
        payload: dict[str, Any] = {
            "ok": True,
            "status": computation.get("status"),
            "result": computation,
            "estimate": computation.get("estimate"),
            "estimate_float": computation.get("estimate_float"),
            "variance": computation.get("variance"),
            "variance_float": computation.get("variance_float"),
            "conflict_ids": computation.get("conflict_ids", []),
        }
        payload.update(extra)
        payload.update(state)
        return jsonify(payload)

    @app.errorhandler(StorageError)
    def handle_storage_error(exc: StorageError):
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "status_code": getattr(exc, "status_code", 400),
                    }
                ),
                getattr(exc, "status_code", 400),
            )
        try:
            return render_page(error=str(exc), status_code=getattr(exc, "status_code", 400))
        except Exception:
            return str(exc), getattr(exc, "status_code", 400)

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        if wants_json():
            return jsonify({"ok": False, "error": exc.description}), exc.code or 500
        return (
            render_template(
                "index.html",
                state=None,
                computation=None,
                error=exc.description,
                success=None,
                report_form={
                    "report_id": "",
                    "mean": "",
                    "buildings": "",
                    "selected": [],
                },
                building_text="",
                variance_values=[""] * storage.MAX_BUILDINGS,
                max_buildings=storage.MAX_BUILDINGS,
                max_reports=storage.MAX_REPORTS,
            ),
            exc.code or 500,
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc: Exception):
        # Storage.transaction has already rolled back when the failure occurred
        # inside a write. Render the last committed SQLite state.
        app.logger.exception("request failed: %s", exc)
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "写入或核算失败，原有数据与合算状态已保留。",
                        "detail": str(exc),
                    }
                ),
                500,
            )
        try:
            return render_page(
                error="写入或核算失败，原有数据与合算状态已保留。",
                status_code=500,
            )
        except Exception:
            return "写入失败，原有状态已保留。", 500

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "building-insulation-accounting"})

    @app.get("/")
    @app.get("/accounting")
    @app.get("/calculate")
    def index():
        edit_id = request.args.get("edit").strip() if request.args.get("edit") else None
        return render_page(edit_id=edit_id)

    @app.get("/api/state")
    @app.get("/api")
    @app.get("/api/accounting")
    def api_state():
        state = storage.get_state(get_db())
        return json_state(state)

    @app.get("/api/computation")
    @app.get("/api/result")
    @app.get("/api/combine")
    def api_computation():
        state = storage.get_state(get_db())
        return json_state(state)

    @app.get("/result")
    def result_page():
        return render_page()

    @app.post("/api/buildings")
    @app.post("/api/building")
    @app.put("/api/buildings")
    def api_save_buildings():
        if request.is_json:
            items = building_inputs_from_json(exact_json())
        else:
            items, _raw_text = building_inputs_from_form()
        state = storage.replace_buildings(get_db(), items)
        return json_state(state)

    @app.post("/buildings")
    def save_buildings():
        try:
            items, raw_text = building_inputs_from_form()
            state = storage.replace_buildings(get_db(), items)
        except StorageError as exc:
            if wants_json():
                raise
            ordered_values = request.form.getlist("variance")
            if any(str(v).strip() for v in ordered_values):
                raw_text = "\n".join(str(v) for v in ordered_values)
                variance_values = [str(v) for v in ordered_values]
            else:
                raw_text = (
                    request.form.get("variances_bulk")
                    or request.form.get("variances")
                    or ""
                )
                variance_values = [
                    token
                    for token in re.split(r"[,，;；、\s]+", raw_text)
                    if token.strip()
                ]
            return render_page(
                error=str(exc),
                building_text=raw_text,
                variance_values=variance_values,
                status_code=exc.status_code,
            )
        if wants_json():
            return json_state(state)
        return render_page(
            success="本轮楼宇正方差已保存，核算状态已在同一事务中更新。"
        )

    def submit_report_core(url_report_id: str | None = None):
        if request.is_json:
            fields = report_fields_from_json(exact_json(), url_report_id)
        else:
            fields = report_fields_from_form(url_report_id)

        state = storage.upsert_report(
            get_db(),
            report_id=fields["report_id"],
            mean=fields["mean"],
            mean_text=fields["mean_text"],
            building_tokens=fields["building_tokens"],
        )
        return state, fields

    @app.post("/api/reports")
    @app.post("/api/report")
    @app.put("/api/reports/<string:url_report_id>")
    @app.post("/api/reports/<string:url_report_id>")
    @app.put("/api/reports/<string:url_report_id>/replace")
    @app.post("/api/reports/<string:url_report_id>/replace")
    def api_submit_report(url_report_id: str | None = None):
        state, _fields = submit_report_core(url_report_id)
        return json_state(state)

    @app.post("/reports")
    @app.post("/reports/<string:url_report_id>")
    @app.post("/reports/<string:url_report_id>/replace")
    @app.post("/reports/<string:url_report_id>/update")
    def submit_report(url_report_id: str | None = None):
        try:
            state, fields = submit_report_core(url_report_id)
        except StorageError as exc:
            if wants_json():
                raise
            # Re-parse form values if possible for sticky form rendering.
            try:
                form = report_fields_from_form(url_report_id)["form"]
            except Exception:
                form = None
            return render_page(
                error=str(exc), report_form=form, status_code=exc.status_code
            )

        if wants_json():
            return json_state(state)
        message = (
            f"报告 {state['saved_report_id']} 已作为新稿录入，合算状态已同步。"
            if not state.get("replaced")
            else f"报告 {state['saved_report_id']} 已合规重报并替换旧稿，合算状态已同步。"
        )
        return render_page(success=message)

    @app.get("/api/reports/<string:report_id>")
    def api_get_report(report_id: str):
        state = storage.get_state(get_db())
        for report in state["reports"]:
            if report["report_id"] == report_id:
                return jsonify({"ok": True, "report": report, "computation": state["computation"]})
        raise storage.NotFoundError(f"找不到报告 {report_id}")

    @app.delete("/api/reports/<string:report_id>")
    @app.post("/api/reports/<string:report_id>/delete")
    def api_delete_report(report_id: str):
        state = storage.delete_report(get_db(), report_id)
        return json_state(state)

    @app.post("/reports/delete")
    def delete_report_by_form():
        report_id = (
            request.form.get("report_id")
            or request.form.get("id")
            or request.args.get("report_id")
            or ""
        )
        state = storage.delete_report(get_db(), str(report_id))
        if wants_json():
            return json_state(state)
        return render_page(
            success=f"报告 {report_id} 已删除，合算状态已在同一事务中更新。"
        )

    @app.delete("/reports/<string:report_id>")
    @app.post("/reports/<string:report_id>/delete")
    def delete_report(report_id: str):
        state = storage.delete_report(get_db(), report_id)
        if wants_json():
            return json_state(state)
        return render_page(
            success=f"报告 {report_id} 已删除，合算状态已在同一事务中更新。"
        )

    @app.post("/api/recompute")
    @app.post("/recompute")
    def recompute():
        conn = get_db()
        with storage.transaction(conn):
            storage.persist_computation(conn)
        if wants_json() or request.path.startswith("/api/"):
            return json_state(storage.get_state(conn))
        return render_page(success="已使用当前 SQLite 中的数据重新核算。")

    @app.post("/api/reset")
    def api_reset():
        state = storage.reset_all(get_db())
        return json_state(state)

    @app.post("/reset")
    def reset():
        storage.reset_all(get_db())
        return render_page(success="已清空本轮数据。")

    @app.template_filter("frac")
    def frac_filter(value: Any) -> str:
        from accounting import format_fraction

        return format_fraction(value) if hasattr(value, "denominator") else str(value)

    @app.template_filter("decimal")
    def decimal_filter(value: Any) -> str:
        from accounting import AccountingInputError, format_decimal, parse_rational

        try:
            return format_decimal(parse_rational(value))
        except AccountingInputError:
            return ""

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=port, debug=False)
