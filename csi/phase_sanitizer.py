#!/usr/bin/env python3
"""
phase_sanitizer.py — Sanitizzazione della fase CSI.

Steps:
  1. Unwrapping (rimuove salti 2π lungo i subcarrier)
  2. Outlier removal (Z-score + interpolazione lineare)
  3. Linear detrend (OLS per-frame: rimuove CFO costante + STO lineare;
                     riferimento: Sen et al., MobiSys 2012)
  4. Moving average smoothing

Dependency: numpy (scipy opzionale per unwrap alternativo).
Adattato da RuView (github.com/ruvnet/RuView) PhaseSanitizer, esteso con
detrend lineare per supportare ML feature basate su fase.
"""

from __future__ import annotations

import numpy as np
from typing import Optional


class PhaseSanitizer:
    """
    Sanitizza dati di fase CSI.

    Pipeline (in ordine):
        1. unwrap        — rimuove salti 2π lungo i subcarrier
        2. outlier       — Z-score per riga + interpolazione lineare
        3. detrend       — sottrae rampa lineare per-frame (rimuove CFO+STO,
                           vedi Sen et al., MobiSys 2012; Ma et al., CSUR 2019)
        4. smooth        — moving average lungo i subcarrier

    Parameters
    ----------
    unwrap_method : str
        Metodo di unwrapping: 'numpy' (default), 'scipy'.
    outlier_threshold : float
        Soglia Z-score per outlier (default 3.0).
    detrend : bool
        Se True (default), sottrae rampa lineare per-frame per rimuovere
        Carrier Frequency Offset (intercept costante) + Sampling Time Offset
        (slope lineare nei subcarrier). Disattivare solo per analisi legacy o
        per debugging del path raw.
    smoothing_window : int
        Finestra moving average (default 5, 0=disabilita).
    """

    def __init__(
        self,
        unwrap_method: str = "numpy",
        outlier_threshold: float = 3.0,
        detrend: bool = True,
        smoothing_window: int = 5,
    ):
        if unwrap_method not in ("numpy", "scipy"):
            raise ValueError(f"unwrap_method sconosciuto: {unwrap_method}")
        if outlier_threshold <= 0:
            raise ValueError("outlier_threshold deve essere > 0")
        if smoothing_window < 0:
            raise ValueError("smoothing_window deve essere >= 0")

        self._unwrap_method = unwrap_method
        self._outlier_threshold = outlier_threshold
        self._detrend_enabled = detrend
        self._smoothing_window = smoothing_window

        # Statistiche
        self.total_processed = 0
        self.outliers_removed = 0

    # ----------------------------------------------------------------
    # Pipeline completa
    # ----------------------------------------------------------------

    def sanitize(self, phase: np.ndarray) -> np.ndarray:
        """
        Applica l'intera pipeline: unwrap → outlier → detrend → smooth.

        Parameters
        ----------
        phase : np.ndarray
            Fase raw, shape (n_frames, n_subcarriers).

        Returns
        -------
        np.ndarray
            Fase sanitizzata, stessa shape.
        """
        if phase.size == 0:
            return phase
        self.total_processed += 1

        # 1. Unwrap
        cleaned = self.unwrap_phase(phase)

        # 2. Outlier removal (PRIMA del detrend: rende OLS più robusta)
        cleaned = self.remove_outliers(cleaned)

        # 3. Linear detrend (rimuove CFO+STO per-frame)
        if self._detrend_enabled:
            cleaned = self.detrend_phase(cleaned)

        # 4. Smoothing
        cleaned = self.smooth_phase(cleaned)

        return cleaned

    # ----------------------------------------------------------------
    # Step 1: Unwrap
    # ----------------------------------------------------------------

    def unwrap_phase(self, phase: np.ndarray) -> np.ndarray:
        """Rimuove salti 2π lungo l'asse delle subcarrier (axis=1)."""
        if phase.size == 0:
            return phase

        if self._unwrap_method == "numpy":
            return np.unwrap(phase, axis=1)
        else:
            # scipy.unwrap è equivalente per axis=1
            try:
                from scipy import signal as _signal
                return _signal.unwrap(phase, axis=1)
            except ImportError:
                return np.unwrap(phase, axis=1)

    # ----------------------------------------------------------------
    # Step 2: Outlier removal
    # ----------------------------------------------------------------

    def remove_outliers(self, phase: np.ndarray) -> np.ndarray:
        """
        Rileva outlier via Z-score per riga e li interpola linearmente.

        Parameters
        ----------
        phase : np.ndarray
            Fase unwrapped, shape (n_frames, n_subcarriers).

        Returns
        -------
        np.ndarray
            Fase senza outlier, stessa shape.
        """
        if phase.size == 0:
            return phase

        outlier_mask = self._detect_outliers(phase)
        return self._interpolate_outliers(phase, outlier_mask)

    def _detect_outliers(self, phase: np.ndarray) -> np.ndarray:
        """Z-score per riga: |z| > soglia → outlier."""
        mean = np.mean(phase, axis=1, keepdims=True)
        std = np.std(phase, axis=1, keepdims=True) + 1e-12
        z_scores = np.abs((phase - mean) / std)
        mask = z_scores > self._outlier_threshold
        self.outliers_removed += int(np.sum(mask))
        return mask

    @staticmethod
    def _interpolate_outliers(
        phase: np.ndarray, mask: np.ndarray
    ) -> np.ndarray:
        """Interpolazione lineare delle posizioni outlier."""
        cleaned = phase.copy()
        for i in range(phase.shape[0]):
            outlier_idx = np.where(mask[i])[0]
            if len(outlier_idx) == 0:
                continue
            valid_idx = np.where(~mask[i])[0]
            if len(valid_idx) < 2:
                # Troppo pochi punti validi — lascia invariato
                continue
            cleaned[i, outlier_idx] = np.interp(
                outlier_idx, valid_idx, phase[i, valid_idx]
            )
        return cleaned

    # ----------------------------------------------------------------
    # Step 3: Linear detrend (CFO + STO removal)
    # ----------------------------------------------------------------

    def detrend_phase(self, phase: np.ndarray) -> np.ndarray:
        """
        Sottrae rampa lineare per-frame nei subcarrier.

        Modella la fase misurata come:

            φ_meas[k] = φ_true[k] + 2π·k·(δ_STO/N) − 2π·δ_CFO − β + ε

        e rimuove i due termini hardware-dependent (STO = pendenza, CFO+β =
        intercept) tramite regressione OLS lungo `k`. Quello che resta è una
        stima di φ_true[k] a meno di un offset globale costante (non
        rilevante per feature statistiche).

        Riferimento canonico: Sen, Radunovic, Choudhury, Minka, "You are
        Facing the Mona Lisa: Spot Localization using PHY Layer Information",
        MobiSys 2012, §4.2 "Phase calibration".

        Parameters
        ----------
        phase : np.ndarray
            Fase unwrapped, shape (n_frames, n_subcarriers).

        Returns
        -------
        np.ndarray
            Fase con rampa lineare per-frame sottratta, stessa shape e dtype.

        Notes
        -----
        OLS è vulnerabile agli outlier residui; assumiamo che `remove_outliers`
        sia già stato chiamato a monte. Per subcarrier ai bordi banda (basso
        SNR intrinseco), la pendenza stimata può essere distorta — una
        subcarrier-selection più severa è un upgrade separato.
        """
        if phase.size == 0:
            return phase
        if phase.ndim != 2:
            raise ValueError(
                f"detrend_phase: atteso shape 2D (n_frames, n_subcarriers), "
                f"ricevuto {phase.shape}"
            )
        _, n_sub = phase.shape
        if n_sub < 2:
            # < 2 subcarrier: regressione degenere, nessuna rampa da togliere.
            return phase

        k = np.arange(n_sub, dtype=np.float64)
        k_mean = k.mean()
        k_centered = k - k_mean
        denom = float(np.sum(k_centered * k_centered))
        if denom < 1e-12:
            # Tutti i k uguali (impossibile per n_sub>=2): no-op difensivo.
            return phase

        phase_f = phase.astype(np.float64)
        # slope_i = Σ_k (k - k̄) · y_i[k]  /  Σ_k (k - k̄)²    (forma compatta OLS)
        slopes = (phase_f * k_centered[None, :]).sum(axis=1) / denom
        # intercept_i = ȳ_i − slope_i · k̄
        intercepts = phase_f.mean(axis=1) - slopes * k_mean
        # Costruisce la matrice della rampa (n_frames × n_sub) e sottrae.
        ramp = slopes[:, None] * k[None, :] + intercepts[:, None]
        return (phase_f - ramp).astype(phase.dtype)

    # ----------------------------------------------------------------
    # Step 4: Smoothing
    # ----------------------------------------------------------------

    def smooth_phase(self, phase: np.ndarray) -> np.ndarray:
        """Moving average lungo axis=1."""
        if phase.size == 0 or self._smoothing_window < 2:
            return phase
        return self._moving_average(phase, self._smoothing_window)

    @staticmethod
    def _moving_average(x: np.ndarray, window: int) -> np.ndarray:
        """Moving average 1D via convoluzione."""
        kernel = np.ones(window) / window
        # convolve 2D lungo axis=1 con 'same' output
        padded = np.pad(x, ((0, 0), (window // 2, window // 2)), mode="edge")
        smoothed = np.apply_along_axis(
            lambda row: np.convolve(row, kernel, mode="valid"), axis=1, arr=padded
        )
        return smoothed

    # ----------------------------------------------------------------
    # Helper: differenza di fase tra frame consecutivi
    # ----------------------------------------------------------------

    @staticmethod
    def phase_difference(phase: np.ndarray) -> np.ndarray:
        """
        Differenza di fase tra frame consecutivi (axis=0).
        Output shape: (n_frames-1, n_subcarriers).
        """
        if phase.shape[0] < 2:
            return np.array([[]])
        diff = np.diff(phase, axis=0)
        # Normalizza in [-π, π]
        return (diff + np.pi) % (2 * np.pi) - np.pi
