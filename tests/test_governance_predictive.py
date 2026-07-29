"""Smoke test for predictive governance features."""
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "core/autonomy")

def main():
    from mike_governance import TrendTracker, PredictiveEngine, SystemSnapshot

    t = TrendTracker(window=12)

    # Simulate rising VRAM over 10 cycles
    for i in range(10):
        t.add(SystemSnapshot(
            timestamp=f"2026-01-01T00:{i*5:02d}:00Z",
            gpu_vram_pct=60 + i * 3,
            disk_free_gb=100 - i * 8,
            gpu_temp=55 + i * 2.5,
        ))

    current_vram = t.latest("gpu_vram_pct")
    predicted_vram = t.predict("gpu_vram_pct", 6)
    print(f"[1] VRAM atual: {current_vram:.0f}%, Predito (30min): {predicted_vram:.0f}%")
    assert predicted_vram is not None
    assert predicted_vram > current_vram, "Should predict rising VRAM"

    current_disk = t.latest("disk_free_gb")
    predicted_disk = t.predict("disk_free_gb", 12)
    print(f"[2] Disco atual: {current_disk:.0f}GB, Predito (1h): {predicted_disk:.0f}GB")
    assert predicted_disk < current_disk, "Should predict falling disk"

    eta = t.time_until_threshold("gpu_vram_pct", 90.0)
    print(f"[3] ETA para 90% VRAM: {eta:.1f} ciclos ({eta*5:.0f} min)")

    # Predictive engine
    pe = PredictiveEngine(t)
    forecasts = pe.forecast()
    print(f"[4] Forecasts gerados: {len(forecasts)}")
    for d in forecasts:
        print(f"    {d.severity}: {d.issue}")
        print(f"    Evidence: {'; '.join(d.evidence)}")

    # Verify that predictive engine catches the rising VRAM
    assert len(forecasts) >= 1, "Should generate at least one forecast"

    # Trend direction
    assert t.trend("gpu_vram_pct") == "rising"
    assert t.trend("disk_free_gb") == "falling"
    print(f"[5] Trends: VRAM={t.trend('gpu_vram_pct')}, Disk={t.trend('disk_free_gb')}")

    # Rate of change
    vram_rate = t.rate_of_change("gpu_vram_pct")
    disk_rate = t.rate_of_change("disk_free_gb")
    print(f"[6] Rate: VRAM +{vram_rate:.1f}%/ciclo, Disk {disk_rate:.1f}GB/ciclo")

    print("\n*** ALL PREDICTIVE GOVERNANCE TESTS PASSED ***")
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
