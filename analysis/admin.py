from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin

from analysis.models import Analysis


@admin.register(Analysis)
class AnalysisAdmin(ModelAdmin):
    """Read-only. Editing a stored result by hand would put a hand-edited reading on a
    screen with nothing to say it had been touched."""

    list_display = ("study", "status", "failure", "attempts", "requested_at", "completed_at")
    list_filter = ("status", "failure")
    search_fields = ("study__id", "study__anonymous_id")
    readonly_fields = ("signal_preview", "quality_summary") + tuple(field.name for field in Analysis._meta.fields)
    fieldsets = (
        ("Señal digitalizada", {"fields": ("signal_preview", "quality_summary")}),
        ("Registro", {"fields": tuple(field.name for field in Analysis._meta.fields)}),
    )

    @admin.display(description="trazado")
    def signal_preview(self, obj: Analysis) -> str:
        """The stored EcgSignal drawn as one SVG per lead, 25 mm/s and 10 mm/mV at 2 px/mm.

        A JSON blob of 60,000 samples says nothing to the eye; the same data as a strip does.
        Gaps stay gaps: each segment is its own polyline, so nothing is drawn where the
        lead was not printed. Read-only, like everything else here.
        """
        signal = obj.signal or (obj.payload or {}).get("signal")
        if not signal or not signal.get("leads"):
            return "sin señal guardada"
        fs = float(signal.get("samplingRateHz") or 500)
        duration = float(signal.get("durationSeconds") or 10)
        px_per_mm = 2.0
        px_per_s, px_per_mv = 25 * px_per_mm, 10 * px_per_mm
        width, row = duration * px_per_s, 3 * px_per_mv
        rows = []
        for lead in signal["leads"]:
            grid = "".join(
                f'<line x1="{x:.0f}" y1="0" x2="{x:.0f}" y2="{row:.0f}" stroke="#e8c6cb" stroke-width="{1 if i % 5 else 1.5}"/>'
                for i, x in enumerate(px_per_mm * k for k in range(int(duration * 25) + 1))
            )
            polys = []
            for seg in lead["segments"]:
                values = seg["values"]
                step = max(1, len(values) // 1200)  # keep the page light; the CSV is the full record
                pts = " ".join(
                    f"{(seg['startSecond'] + i / fs) * px_per_s:.1f},{row / 2 - v * px_per_mv:.1f}"
                    for i, v in enumerate(values)
                    if i % step == 0
                )
                polys.append(f'<polyline points="{pts}" fill="none" stroke="#1a1a1a" stroke-width="1.2"/>')
            rows.append(
                format_html(
                    '<div style="margin:4px 0"><span style="font-family:monospace;color:#666">{}</span>'
                    '<svg width="{}" height="{}" style="display:block;background:#fff7f7">{}{}</svg></div>',
                    lead["name"],
                    int(width),
                    int(row),
                    format_html(grid),
                    format_html("".join(polys)),
                )
            )
        return format_html('<div style="overflow-x:auto">{}</div>', format_html_join("", "{}", ((r,) for r in rows)))

    @admin.display(description="calidad de la digitalización")
    def quality_summary(self, obj: Analysis) -> str:
        """What the pipeline reported about the read, one line per fact, before the JSON."""
        d = obj.diagnostics or {}
        dig, q = d.get("digitization") or {}, d.get("signal_quality") or {}
        cost = dig.get("matching_cost")
        facts = [
            ("layout", dig.get("lead_layout") or "-"),
            ("coste de ajuste", f"{cost:.3f}" if isinstance(cost, (int, float)) else "-"),
            ("derivaciones con señal", f"{len(q.get('leads_with_signal', []))} de 12"),
            ("tiras completas", ", ".join(q.get("full_length_leads", [])) or "-"),
            ("usadas para el ritmo", ", ".join(q.get("selected_leads", [])) or "-"),
            ("puertas que saltaron", ", ".join(d.get("gates") or []) or "ninguna"),
            ("huecos cerrados (ms)", str(d.get("signal_bridging") or "ninguno")),
            ("versión del pipeline", d.get("pipeline_version") or "-"),
        ]
        return format_html_join("", "<div><b>{}</b>: {}</div>", ((k, v) for k, v in facts))

    def has_add_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # type: ignore[no-untyped-def]
        return False
