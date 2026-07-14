"""Reliable intake entry points for private source material."""

__all__ = ["PdfIntakeError", "intake_pdf"]


def __getattr__(name):
    if name in __all__:
        from .pdf_intake import PdfIntakeError, intake_pdf

        return {"PdfIntakeError": PdfIntakeError, "intake_pdf": intake_pdf}[name]
    raise AttributeError(name)
