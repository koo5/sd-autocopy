#!/usr/bin/env -S uv run --locked --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow", "pytest"]
#
# [tool.uv]
# exclude-newer = "2026-04-19T00:00:00Z"
# ///
"""Tests for sd-autocopy-copy: naming from EXIF (UTC, camera token, camera
basename), the two-stage copy, atomic destinations, the duplicate guard, and
abort semantics. End-to-end runs use a fake card of tiny JPEGs stamped with
exiftool; the pure naming functions are tested directly.

Run:  ./test_copy.py   # or: pytest tests/test_copy.py -v
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "sd-autocopy-copy"
_spec = importlib.util.spec_from_loader(
	"sdc", importlib.machinery.SourceFileLoader("sdc", str(SCRIPT)))
sdc = importlib.util.module_from_spec(_spec)
sys.modules["sdc"] = sdc          # dataclasses look the module up by name
_spec.loader.exec_module(sdc)
Meta = sdc.Meta

SERIAL = "363055000036"
UTC = timezone.utc


# --------------------------------------------------------------------------
# fixtures: a fake card, a config, an alert sink
# --------------------------------------------------------------------------

def stamp(path: Path, *, dto: str, subsec: str = "99", offset: str | None = "+02:00",
		  model: str = "Canon EOS 5DS", serial: str = SERIAL) -> None:
	args = ["exiftool", "-q", "-overwrite_original", f"-Model={model}",
			f"-EXIF:SerialNumber={serial}", f"-DateTimeOriginal={dto}",
			f"-SubSecTimeOriginal={subsec}"]
	if offset is not None:
		args.append(f"-OffsetTimeOriginal={offset}")
	subprocess.run(args + [str(path)], check=True, capture_output=True)


def jpeg(card: Path, rel: str, *, mtime: float, **exif) -> Path:
	p = card / rel
	p.parent.mkdir(parents=True, exist_ok=True)
	Image.new("RGB", (2, 2), (120, 90, 60)).save(p, "JPEG")
	if exif:
		stamp(p, **exif)
	os.utime(p, (mtime, mtime))
	return p


def epoch(s: str) -> float:
	"""ISO-8601 with zone -> epoch seconds."""
	return datetime.fromisoformat(s).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
	"""Config + env for a hermetic run; returns (card, target, backup, alerts)."""
	card = tmp_path / "card"
	target = tmp_path / "autocopy"
	backup = tmp_path / "autobackup"
	alerts = tmp_path / "alerts.txt"
	alert_py = tmp_path / "alert.py"
	alert_py.write_text(
		"import sys, pathlib\n"
		f"pathlib.Path({str(alerts)!r}).open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
	)
	cfg = tmp_path / "env"
	cfg.write_text(
		f"AUTOCOPY_TARGET={target}\nAUTOCOPY_BACKUP={backup}\n"
		f"ALERT_PY={alert_py}\nWORKERS=2\nCAM_ALIAS_{SERIAL}=5ds\n"
	)
	monkeypatch.setenv("SD_AUTOCOPY_CONFIG", str(cfg))
	monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
	card.mkdir()
	return card, target, backup, alerts, cfg


def run(card: Path, *extra: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
	e = dict(os.environ)
	if env_extra:
		e.update(env_extra)
	return subprocess.run([sys.executable, str(SCRIPT), *extra, str(card)],
						  capture_output=True, text=True, env=e)


def names(root: Path) -> list[str]:
	return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# --------------------------------------------------------------------------
# pure naming
# --------------------------------------------------------------------------

def test_instant_converts_the_stamped_offset_to_utc():
	inst, ms, warn = sdc.capture_instant(
		Meta(dto="2026:09:16 01:30:00", subsec="99", offset="+02:00"), 0)
	assert (inst, ms, warn) == (datetime(2026, 9, 15, 23, 30, tzinfo=UTC), 990, None)


def test_millis_are_right_padded_to_three_digits():
	assert sdc._millis("99") == 990
	assert sdc._millis("7") == 700
	assert sdc._millis("1234") == 123
	assert sdc._millis("") == 0


def test_no_offset_is_taken_as_utc_and_flagged():
	inst, ms, warn = sdc.capture_instant(Meta(dto="2026:09:01 12:57:24", subsec="7"), 0)
	assert inst == datetime(2026, 9, 1, 12, 57, 24, tzinfo=UTC)
	assert ms == 700 and "no timezone offset" in warn


def test_mov_create_date_is_utc_without_a_warning():
	inst, ms, warn = sdc.capture_instant(Meta(create_date="2026:08:21 20:41:41+02:00"), 0)
	assert (inst, ms, warn) == (datetime(2026, 8, 21, 18, 41, 41, tzinfo=UTC), 0, None)
	inst, _, warn = sdc.capture_instant(Meta(create_date="2026:08:21 18:41:41"), 0)
	assert (inst, warn) == (datetime(2026, 8, 21, 18, 41, 41, tzinfo=UTC), None)


def test_no_exif_time_falls_back_to_mtime_and_flags():
	inst, ms, warn = sdc.capture_instant(Meta(), epoch("2026-08-22T11:56:05.250+02:00"))
	assert inst == datetime(2026, 8, 22, 9, 56, 5, 250000, tzinfo=UTC)
	assert ms == 250 and "no EXIF time" in warn


def test_an_unset_clock_is_not_a_date():
	# Canon writes 0000:00:00 00:00:00 after a battery pull. Not a crash:
	# the value parses to nothing and the mtime fallback takes over.
	assert sdc._parse_exif_dt("0000:00:00 00:00:00") == (None, "")
	inst, _, warn = sdc.capture_instant(
		Meta(dto="0000:00:00 00:00:00", subsec="00", offset="+00:00"),
		epoch("2026-08-22T09:56:05+00:00"))
	assert inst == datetime(2026, 8, 22, 9, 56, 5, tzinfo=UTC) and "no EXIF time" in warn


def test_camera_token_alias_default_and_fallback():
	m = Meta(model="Canon EOS 5DS", serial=SERIAL)
	assert sdc.camera_token(m, {SERIAL: "5ds"}) == ("5ds", None)
	assert sdc.camera_token(m, {}) == ("5DS-363055000036", None)
	assert sdc.camera_token(Meta(model="Canon EOS 5D Mark IV", serial="1 2"), {})[0] == "5D-Mark-IV-1-2"
	tok, warn = sdc.camera_token(Meta(), {})
	assert tok == "nocam" and warn


def test_name_shape():
	inst, ms, _ = sdc.capture_instant(Meta(dto="2026:09:15 14:57:24", subsec="99", offset="+02:00"), 0)
	assert sdc.build_name(inst, ms, "5ds", "AAAE2634.CR2") == "20260915_125724_990Z_5ds_AAAE2634.CR2"


def test_config_rejects_an_alias_with_an_underscore(tmp_path):
	cfg = tmp_path / "env"
	cfg.write_text(f"CAM_ALIAS_{SERIAL}=bad_one\n")
	with pytest.raises(sdc.AbortError, match="must match"):
		sdc.load_config(cfg)
	cfg.write_text(f"CAM_ALIAS_{SERIAL}=5ds   # comment\nWORKERS=3\n")
	c = sdc.load_config(cfg)
	assert c.aliases == {SERIAL: "5ds"} and c.workers == 3


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------

def test_a_card_lands_in_both_trees_named_by_utc_instant(env):
	card, target, backup, alerts, _ = env
	# 01:30 +02:00 on the 16th is 23:30Z on the 15th: the UTC day wins, and
	# the backup is re-homed from the mtime guess (set a day off on purpose).
	jpeg(card, "DCIM/100EOS5D/AAAE2634.JPG", mtime=epoch("2026-09-17T00:00:00+00:00"),
		 dto="2026:09:16 01:30:00", subsec="99", offset="+02:00")
	r = run(card)
	assert r.returncode == 0, r.stderr
	assert names(target) == ["2026-09-15/jpg/20260915_233000_990Z_5ds_AAAE2634.JPG"]
	assert names(backup) == ["2026-09-15/DCIM/100EOS5D/AAAE2634.JPG"]
	# Card file retired only after both copies.
	assert names(card) == ["DCIM/100EOS5D/old/AAAE2634.JPG"]
	assert not alerts.exists()
	# The mirror keeps the card mtime (the pipeline reads mtime as a content fingerprint).
	m = target / "2026-09-15/jpg/20260915_233000_990Z_5ds_AAAE2634.JPG"
	assert int(m.stat().st_mtime) == int(epoch("2026-09-17T00:00:00+00:00"))
	assert not (m.stat().st_mode & 0o111)


def test_names_do_not_depend_on_the_host_timezone(env):
	card, target, backup, _, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 14:57:24", subsec="99", offset="+02:00")
	assert run(card, env_extra={"TZ": "Asia/Tokyo"}).returncode == 0
	got_tokyo = names(target)
	for p in (target, backup):
		subprocess.run(["rm", "-rf", str(p)], check=True)
	(card / "DCIM/100EOS5D/old/AAAE0001.JPG").rename(card / "DCIM/100EOS5D/AAAE0001.JPG")
	assert run(card, env_extra={"TZ": "America/Los_Angeles"}).returncode == 0
	assert names(target) == got_tokyo == ["2026-09-15/jpg/20260915_125724_990Z_5ds_AAAE0001.JPG"]


def test_a_counter_wrap_across_two_folders_merges_in_capture_order(env):
	card, target, _, _, _ = env
	t0 = "2026:09:15 14:57:24"
	jpeg(card, "DCIM/100EOS5D/AAAE9999.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto=t0, subsec="10", offset="+02:00")
	jpeg(card, "DCIM/101EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:25+00:00"),
		 dto="2026:09:15 14:57:25", subsec="30", offset="+02:00")
	assert run(card).returncode == 0
	assert names(target) == [
		"2026-09-15/jpg/20260915_125724_100Z_5ds_AAAE9999.JPG",
		"2026-09-15/jpg/20260915_125725_300Z_5ds_AAAE0001.JPG",
	]


def test_unaliased_body_gets_model_and_serial(env):
	card, target, _, _, cfg = env
	cfg.write_text(cfg.read_text().replace(f"CAM_ALIAS_{SERIAL}=5ds\n", ""))
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 12:57:24", subsec="00", offset="+00:00")
	assert run(card).returncode == 0
	assert names(target) == ["2026-09-15/jpg/20260915_125724_000Z_5DS-363055000036_AAAE0001.JPG"]


def test_zone_less_frames_are_copied_as_utc_and_alerted(env):
	card, target, _, alerts, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 14:57:24", subsec="99", offset=None)
	r = run(card)
	assert r.returncode == 0
	assert names(target) == ["2026-09-15/jpg/20260915_145724_990Z_5ds_AAAE0001.JPG"]
	assert "timezone" in alerts.read_text() and "AAAE0001.JPG" in alerts.read_text()


def test_an_unset_camera_clock_is_copied_on_mtime_and_alerted(env):
	card, target, _, alerts, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="0000:00:00 00:00:00", subsec="00", offset="+00:00")
	r = run(card)
	assert r.returncode == 0, r.stderr
	assert names(target) == ["2026-09-15/jpg/20260915_125724_000Z_5ds_AAAE0001.JPG"]
	assert names(card) == ["DCIM/100EOS5D/old/AAAE0001.JPG"]
	assert "no EXIF time" in alerts.read_text()


def test_mov_routes_to_mov_dir_on_mtime_when_it_has_no_exif(env):
	card, target, _, alerts, _ = env
	p = card / "DCIM/100EOS5D/AAAE0002.MOV"
	p.parent.mkdir(parents=True)
	p.write_bytes(b"not really a movie")
	os.utime(p, (epoch("2026-09-15T12:57:24.5+00:00"),) * 2)
	assert run(card).returncode == 0
	assert names(target) == ["2026-09-15/mov/20260915_125724_500Z_nocam_AAAE0002.MOV"]
	assert "no EXIF time" in alerts.read_text()


def test_reinserting_the_card_skips_what_is_already_there(env):
	card, target, backup, alerts, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 12:57:24", subsec="00", offset="+00:00")
	assert run(card).returncode == 0
	# Simulate a run that died after the copies but before retiring the card file.
	(card / "DCIM/100EOS5D/old/AAAE0001.JPG").rename(card / "DCIM/100EOS5D/AAAE0001.JPG")
	before = names(target) + names(backup)
	r = run(card, "-v")
	assert r.returncode == 0 and "1 already present" in r.stderr
	assert names(target) + names(backup) == before
	assert names(card) == ["DCIM/100EOS5D/old/AAAE0001.JPG"]
	assert not alerts.exists()


def test_a_different_file_under_the_same_name_aborts(env):
	card, target, backup, alerts, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 12:57:24", subsec="00", offset="+00:00")
	taken = target / "2026-09-15/jpg/20260915_125724_000Z_5ds_AAAE0001.JPG"
	taken.parent.mkdir(parents=True)
	taken.write_bytes(b"something else entirely")
	r = run(card)
	assert r.returncode == 1
	assert taken.read_bytes() == b"something else entirely"
	assert names(card) == ["DCIM/100EOS5D/AAAE0001.JPG"]     # left on the card
	assert "aborted" in alerts.read_text()


def test_a_stale_partial_is_ignored(env):
	card, target, _, _, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 12:57:24", subsec="00", offset="+00:00")
	d = target / "2026-09-15/jpg"
	d.mkdir(parents=True)
	(d / ".20260915_125724_000Z_5ds_AAAE0001.JPG.partial").write_bytes(b"junk")
	assert run(card).returncode == 0
	assert names(target) == ["2026-09-15/jpg/20260915_125724_000Z_5ds_AAAE0001.JPG"]


def test_a_mirror_failure_aborts_with_the_card_file_in_place(env):
	card, target, backup, alerts, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 12:57:24", subsec="00", offset="+00:00")
	target.mkdir()
	target.chmod(0o555)   # mkdir of the date dir fails
	try:
		r = run(card)
	finally:
		target.chmod(0o755)
	assert r.returncode == 1
	assert names(backup) == ["2026-09-15/DCIM/100EOS5D/AAAE0001.JPG"]
	assert names(card) == ["DCIM/100EOS5D/AAAE0001.JPG"]
	assert "aborted" in alerts.read_text()


def test_dry_run_writes_nothing(env):
	card, target, backup, _, _ = env
	jpeg(card, "DCIM/100EOS5D/AAAE0001.JPG", mtime=epoch("2026-09-15T12:57:24+00:00"),
		 dto="2026:09:15 14:57:24", subsec="99", offset="+02:00")
	r = run(card, "--dry-run")
	assert r.returncode == 0, r.stderr
	assert "20260915_125724_990Z_5ds_AAAE0001.JPG" in r.stdout
	assert not target.exists() and not backup.exists()
	assert names(card) == ["DCIM/100EOS5D/AAAE0001.JPG"]


def test_missing_mount_fails(env):
	card, *_ = env
	assert run(card / "nope").returncode == 1


if __name__ == "__main__":
	sys.exit(pytest.main([__file__, "-v"]))
