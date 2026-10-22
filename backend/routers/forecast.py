import os
import io
import csv
import json
import uuid
import datetime
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from database import get_db
from auth import get_current_user
from models import ForecastRun, Forecast, Recommendation, InventoryUnit
from ml.model_adapter import PlateletDemandModel
from services.decision_engine import evaluate_decision

router = APIRouter(prefix="/banks/{bank_id}/forecast", tags=["forecast"])

MODEL_PATH = os.getenv("MODEL_PATH", os.path.join(os.path.dirname(__file__), "..", "ml", "plateletiq_model.joblib"))

_model_instance = None


def get_model():
    global _model_instance
    if _model_instance is None:
        path = MODEL_PATH
        if not os.path.exists(path):
            raise FileNotFoundError(f"Model not found at {path}. Set MODEL_PATH env var.")
        _model_instance = PlateletDemandModel(path=path)
    return _model_instance


@router.get("/latest")
def get_latest_forecast(bank_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    run = (
        db.query(ForecastRun)
        .filter(ForecastRun.bank_id == bank_id, ForecastRun.status == "SUCCEEDED")
        .order_by(ForecastRun.started_at.desc())
        .first()
    )
    if not run:
        # Fallback 7-day forecast if no DB runs exist yet
        today = datetime.date.today()
        dates = [today + datetime.timedelta(days=i) for i in range(1, 8)]
        q50_base = [21.0, 24.0, 29.0, 27.0, 25.0, 20.0, 31.0]
        rows = []
        for i, d in enumerate(dates):
            q50 = q50_base[i]
            rows.append({
                "date": d.isoformat(),
                "q50": q50,
                "q67": round(q50 * 1.18, 2),
                "q90": round(q50 * 1.40, 2),
                "horizon_day": i + 1,
            })
        return {
            "data": {
                "forecast_run_id": "seed-run-001",
                "model_version": "LASSO v1.4",
                "forecast": rows,
                "generated_at": datetime.datetime.utcnow().isoformat(),
                "metadata": {
                    "mase_on_holdout": 0.734,
                    "mape_on_holdout": 24.58,
                    "mae_on_holdout": 5.303,
                    "n_train_days": 4018,
                    "training_dataset": "Published German hospital demand data (2008–2018)",
                    "india_calibrated": False,
                },
            },
            "error": None,
        }

    forecasts = db.query(Forecast).filter(Forecast.forecast_run_id == run.id).order_by(Forecast.horizon_day.asc()).all()

    return {
        "data": {
            "forecast_run_id": run.id,
            "model_version": run.model_version,
            "forecast": [
                {
                    "date": f.forecast_date.isoformat(),
                    "q50": f.q50,
                    "q67": f.q67,
                    "q90": f.q90,
                    "horizon_day": f.horizon_day,
                }
                for f in forecasts
            ],
            "generated_at": run.completed_at.isoformat() if run.completed_at else run.started_at.isoformat(),
            "metadata": {
                "mase_on_holdout": 0.734,
                "mape_on_holdout": 24.58,
                "mae_on_holdout": 5.303,
                "n_train_days": 4018,
                "training_dataset": "Published German hospital demand data (2008–2018)",
                "india_calibrated": False,
            },
        },
        "error": None,
    }


@router.post("/runs")
def trigger_forecast_run(bank_id: str, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    try:
        model = get_model()
        today = datetime.date.today()
        # Seed historical series of 90 days
        dates = pd.date_range(end=today, periods=90, freq="D")
        history_values = [26 + (i % 7) * 2 for i in range(90)]
        history = pd.Series(history_values, index=dates)

        forecast_dates = [today + datetime.timedelta(days=i) for i in range(1, 8)]
        res = model.predict(history, forecast_dates)

        run = ForecastRun(
            id=str(uuid.uuid4()),
            bank_id=bank_id,
            model_version=model.model_version,
            status="SUCCEEDED",
            started_at=datetime.datetime.utcnow(),
            completed_at=datetime.datetime.utcnow(),
            history_days=90,
        )
        db.add(run)

        for i, item in enumerate(res["forecast"]):
            f = Forecast(
                id=str(uuid.uuid4()),
                bank_id=bank_id,
                forecast_run_id=run.id,
                forecast_date=datetime.date.fromisoformat(item["date"]),
                horizon_day=i + 1,
                q50=item["q50"],
                q67=item["q67"],
                q90=item["q90"],
                model_version=model.model_version,
            )
            db.add(f)

        db.commit()

        return {"data": {"run_id": run.id, "status": "SUCCEEDED"}, "error": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload-history")
async def upload_history(
    bank_id: str,
    file: UploadFile = File(None),
    manual_data: str = Form(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    One-time bootstrap / historical upload: upload historical demand data to generate forecast.
    Accepts CSV (date, units_issued) or JSON manual entry array string.
    Minimum 14 rows required.
    """
    rows = []
    raw_upload, upload_type = None, None

    if file:
        content = await file.read()
        raw_upload, upload_type = content, "text/csv"
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        for i, row in enumerate(reader):
            try:
                date_str = row.get("date", "").strip()
                units_str = row.get("units_issued", "0").strip()
                if not date_str or not units_str:
                    continue
                units = int(units_str)
                if units < 0:
                    raise HTTPException(422, f"Row {i+1}: units_issued cannot be negative ({units})")
                d = datetime.date.fromisoformat(date_str)
                if d > datetime.date.today():
                    raise HTTPException(422, f"Row {i+1}: date cannot be in the future ({date_str})")
                rows.append({"date": date_str, "units_issued": units})
            except ValueError as e:
                raise HTTPException(422, f"Row {i+1}: invalid data format — {e}")

    elif manual_data:
        raw_upload, upload_type = manual_data.encode("utf-8"), "application/json"
        try:
            entries = json.loads(manual_data)
        except json.JSONDecodeError:
            raise HTTPException(422, "Invalid JSON format in manual_data")
        for i, entry in enumerate(entries):
            date_str = entry.get("date", "").strip()
            if not date_str:
                continue
            units = int(entry.get("units_issued", 0))
            if units < 0:
                raise HTTPException(422, f"Entry {i+1}: units_issued cannot be negative")
            rows.append({"date": date_str, "units_issued": units})
    else:
        raise HTTPException(422, "Provide either a CSV file or manual_data JSON string")

    if len(rows) < 14:
        raise HTTPException(422, f"Your CSV has {len(rows)} rows. The model needs at least 14 days of history to detect weekly patterns. Please add more dates.")

    series = pd.Series(
        {pd.Timestamp(r["date"]): float(r["units_issued"]) for r in rows}
    ).sort_index()

    model = get_model()
    today = datetime.date.today()
    forecast_dates = [today + datetime.timedelta(days=i) for i in range(1, 8)]

    try:
        result = model.predict(series, forecast_dates)
    except Exception as e:
        raise HTTPException(500, f"Model inference failed: {str(e)}")

    run = ForecastRun(
        id=str(uuid.uuid4()),
        bank_id=bank_id,
        model_version=result["model_version"],
        status="SUCCEEDED",
        started_at=datetime.datetime.utcnow(),
        completed_at=datetime.datetime.utcnow(),
        history_days=len(rows),
        history_end=str(series.index.max().date()),
    )
    db.add(run)

    forecasts = []
    for i, item in enumerate(result["forecast"]):
        f = Forecast(
            id=str(uuid.uuid4()),
            bank_id=bank_id,
            forecast_run_id=run.id,
            forecast_date=datetime.date.fromisoformat(item["date"]),
            horizon_day=i + 1,
            q50=item["q50"],
            q67=item["q67"],
            q90=item["q90"],
            model_version=result["model_version"],
        )
        db.add(f)
        forecasts.append(item)

    # Supersede existing ACTIVE recommendations and produce new recommendation
    old_recs = db.query(Recommendation).filter(
        Recommendation.bank_id == bank_id,
        Recommendation.status == "ACTIVE"
    ).all()
    for r in old_recs:
        r.status = "SUPERSEDED"

    units = db.query(InventoryUnit).filter(
        InventoryUnit.bank_id == bank_id,
        InventoryUnit.status == "AVAILABLE"
    ).all()
    unit_dicts = [{"expiry_at": u.expiry_at, "status": u.status} for u in units]

    dec = evaluate_decision(forecasts, unit_dicts)
    new_rec = Recommendation(
        id=str(uuid.uuid4()),
        bank_id=bank_id,
        forecast_run_id=run.id,
        action=dec["action"],
        quantity=dec["quantity"],
        reason_summary=dec["reason_summary"],
        drivers_json=json.dumps(dec["drivers"]),
        status="ACTIVE",
    )
    db.add(new_rec)

    db.commit()

    # Keep the exact file the forecast was trained on (Supabase Storage), so
    # every recommendation can be traced back to its input data.
    archived = None
    try:
        from cloud import storage

        if storage.configured() and raw_upload:
            extension = "csv" if upload_type == "text/csv" else "json"
            stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            stored = storage.upload(f"{bank_id}/demand-history/{stamp}-{run.id[:8]}.{extension}", raw_upload, upload_type)
            archived = {"store": "supabase", "path": stored["path"]}
    except Exception as exc:
        print(f"Demand history not archived: {exc!r}")

    return {
        "data": {
            "run_id": run.id,
            "status": "SUCCEEDED",
            "history_days": len(rows),
            "forecast": forecasts,
            "recommendation": {
                "action": dec["action"],
                "quantity": dec["quantity"],
                "reason_summary": dec["reason_summary"],
                "drivers": dec["drivers"],
            },
            "message": "Forecast updated. Overview and Actions now reflect your data.",
            "archived": archived,
        },
        "error": None,
    }

