import csv
import subprocess
import sys
from pathlib import Path

from src.scoring.evaluation_agent import EvaluationAgent, TargetData
from src.scrapers.pretor import PretorParser


ROOT = Path(__file__).resolve().parents[1]


def test_offline_demo(tmp_path):
    destination = tmp_path / "ranking.csv"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/demo.py"), "--output", str(destination)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert "3 of 5 sample plants qualified." in completed.stdout
    with destination.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["plant"] for row in rows] == ["Example Solar B", "Example Solar A", "Example Solar C"]
    assert [row["normalized_score"] for row in rows] == ["72.9", "70.8", "66.7"]


def test_solar_thermal_and_explicit_rooftops_are_filtered(tmp_path):
    source = tmp_path / "registry.csv"
    source.write_text(
        "clave registro,nombre,potencia instalada kw,grupo normativo\n"
        "A,Example Open Field,500,b.1.1\n"
        "B,Example Thermal,500,b.1.2\n"
        "C,Example Rooftop,500,b.1.1\n",
        encoding="utf-8",
    )
    assert [target.source_id for target in PretorParser().parse_file(source)] == ["A"]


def test_scoring_prompt_omits_identifiers():
    target = TargetData(
        id="private-record-id",
        plant_name="private-plant-name",
        owner_name="private-owner-name",
        referencia_catastral="private-cadastral-ref",
        municipality="private-municipality",
        latitude=40.123456,
        longitude=-3.123456,
        capacity_kw=750,
    )
    # Formatting does not need a model client or key.
    agent = EvaluationAgent.__new__(EvaluationAgent)
    prompt = agent._format_target_data(target)
    assert "750.00 kW" in prompt
    for value in (
        target.id, target.plant_name, target.owner_name, target.referencia_catastral,
        target.municipality, "40.123456", "-3.123456",
    ):
        assert value not in prompt
