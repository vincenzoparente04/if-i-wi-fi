"""CSIClassifier — Random Forest for CSI-based presence/activity classification.

Extracted from csi_ml.py.
"""
import json
import os
import sys
import time
from collections import Counter, deque
from statistics import mean, stdev
from typing import Any

from .features import (
    GLOBAL_FEATURE_NAMES, CSI_FEATURE_NAMES, CSI_FEATURE_SIZE,
    SUB_MEAN_PREFIX, SUB_STD_PREFIX, SUB_PHASE_MEAN_PREFIX, SUB_PHASE_STD_PREFIX,
    NUM_CSI_SUBCARRIERS, MAX_SUB,
    extract_csi_profile, csi_window_to_vector,
    _extract_source_profile, _generate_source_feature_names, _prefix_from_value,
    extract_csi_profile_per_source, csi_window_to_vector_per_source,
)

# sklearn: import lazy
_SKLEARN_AVAILABLE = False
_RF_CLASS = None
_train_test_split = None
_accuracy_score = None
_f1_score = None
_confusion_matrix = None
_prfs = None
try:
    from sklearn.ensemble import RandomForestClassifier as _RFC
    from sklearn.model_selection import train_test_split as _train_test_split  # type: ignore
    from sklearn.metrics import (  # type: ignore
        accuracy_score as _accuracy_score,
        f1_score as _f1_score,
        confusion_matrix as _confusion_matrix,
        precision_recall_fscore_support as _prfs,
    )
    _RF_CLASS = _RFC
    _SKLEARN_AVAILABLE = True
except ImportError:
    pass

try:
    import joblib as _joblib
except ImportError:
    _joblib = None

# Model paths
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
CSI_MODEL_PATH = os.path.join(MODEL_DIR, "csi_model.joblib")
POSITIONS_MODEL_PATH = os.path.join(MODEL_DIR, "csi_positions_model.joblib")
POSITIONS_LABELS_PATH = os.path.join(MODEL_DIR, "csi_positions_labels.json")

# Classes
EMPTY = 0
STILL = 1
MOTION = 2
CSI_CLASSES = {EMPTY: "EMPTY", STILL: "STILL", MOTION: "MOTION"}
CSI_LABELS = ["EMPTY", "STILL", "MOTION"]

class CSIClassifier:
    """
    Random Forest multi-classe per classificazione attività da CSI.

    Usage:
        clf = CSIClassifier(window_frames=30)

        # Training
        clf.train(empty_frames, stationary_frames, movement_frames)
        clf.save()

        # Real-time inference
        clf.add_frame(csi_frame_dict)
        if clf.ready:
            probs = clf.predict_proba()
            # → {EMPTY: 0.1, STILL: 0.7, MOTION: 0.2}
    """

    def __init__(self, window_frames: int = 30):
        self.window_frames = window_frames
        self.frame_hist: deque = deque(maxlen=window_frames)
        self._model = None
        self._trained = False
        self._t0 = 0.0
        self._last_features: dict = {}
        self._last_probas: dict = {lbl: 0.0 for lbl in CSI_LABELS}
        self._feature_importance: dict = {}
        self._class_labels: list[str] | None = None  # label personalizzate (posizioni)
        self._known_sources: list[str] = []  # valori sorgente (MAC o source_id)
        self._source_key: str = "mac"  # chiave nel frame dict per grouping
        self._custom_feature_names: list[str] = []  # feature names per modello custom

    # ---- Properties ----

    @property
    def ready(self) -> bool:
        """Pronto per inferenza: finestra piena e modello addestrato."""
        return self._trained and len(self.frame_hist) == self.window_frames

    @property
    def trained(self) -> bool:
        return self._trained

    # ---- Training ----

    @staticmethod
    def _check_sklearn():
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError(
                "scikit-learn non installato.\n"
                "  UNO Q: sudo apt install python3-sklearn python3-joblib\n"
                "  Mac:   pip install scikit-learn joblib"
            )

    @staticmethod
    def _holdout_validate(X: list, y: list, label_map,
                          rf_kwargs: dict, test_size: float = 0.20) -> dict:
        """80/20 stratified holdout validation.

        Allena un modello "shadow" su 80% dei dati, valuta sul 20% rimanente
        e ritorna accuracy, macro-F1, weighted-F1, confusion matrix e
        precision/recall/F1 per classe. Il modello shadow viene scartato:
        il caller deve ri-fittare su tutto il dataset per il modello di
        produzione.

        Se la classe minoritaria ha troppo pochi campioni per supportare
        lo stratified split, ricade su split non-stratified. Se nemmeno
        quello funziona, ritorna {"skipped": True, "reason": ...}.

        Args:
            X: feature matrix (list di list).
            y: vettore label int.
            label_map: dict {int: str} oppure lista indicizzata per nome.
            rf_kwargs: kwargs passati a RandomForestClassifier.
            test_size: frazione test (default 0.20).
        """
        assert _RF_CLASS is not None
        assert _train_test_split is not None
        assert _accuracy_score is not None
        assert _f1_score is not None
        assert _confusion_matrix is not None
        assert _prfs is not None

        n = len(y)
        if n < 5:
            return {"skipped": True, "reason": f"dataset troppo piccolo ({n} campioni)"}

        class_counts = Counter(y)
        min_class = min(class_counts.values())
        # stratify richiede almeno 2 per classe nel test set: min_class * test_size >= 2
        min_for_stratify = max(2, int(round(2.0 / test_size)))
        use_stratify = min_class >= min_for_stratify

        try:
            X_tr, X_te, y_tr, y_te = _train_test_split(
                X, y,
                test_size=test_size,
                random_state=42,
                stratify=y if use_stratify else None,
            )
        except ValueError as e:
            return {"skipped": True, "reason": f"split fallito: {e}"}

        val_model = _RF_CLASS(**rf_kwargs)
        val_model.fit(X_tr, y_tr)
        y_pred = val_model.predict(X_te)

        labels_sorted = sorted(set(y))

        def _name(cls_idx: int) -> str:
            if isinstance(label_map, list):
                return label_map[cls_idx] if 0 <= cls_idx < len(label_map) else str(cls_idx)
            return label_map.get(cls_idx, str(cls_idx))

        acc = float(_accuracy_score(y_te, y_pred))
        macro_f1 = float(_f1_score(y_te, y_pred, average="macro", zero_division=0))
        weighted_f1 = float(_f1_score(y_te, y_pred, average="weighted", zero_division=0))
        cm = _confusion_matrix(y_te, y_pred, labels=labels_sorted)
        p, r, f, support = _prfs(y_te, y_pred, labels=labels_sorted, zero_division=0)

        per_class = {}
        for i, cls in enumerate(labels_sorted):
            per_class[_name(cls)] = {
                "precision": round(float(p[i]), 4),
                "recall":    round(float(r[i]), 4),
                "f1":        round(float(f[i]), 4),
                "support":   int(support[i]),
            }

        return {
            "skipped": False,
            "stratified": use_stratify,
            "n_train": len(X_tr),
            "n_test": len(X_te),
            "accuracy": round(acc, 4),
            "macro_f1": round(macro_f1, 4),
            "weighted_f1": round(weighted_f1, 4),
            "per_class": per_class,
            "confusion_matrix": cm.tolist(),
            "confusion_labels": [_name(c) for c in labels_sorted],
        }

    @staticmethod
    def _print_validation(val: dict) -> None:
        """Stampa leggibile delle metriche di validazione."""
        if val.get("skipped"):
            print(f"\n  [Validation] Skippata: {val.get('reason')}")
            return
        strat = "stratified" if val.get("stratified") else "random"
        print(f"\n  [Validation] holdout 80/20 ({strat}) — "
              f"train={val['n_train']} test={val['n_test']}")
        print(f"    accuracy    : {val['accuracy']:.4f}")
        print(f"    macro F1    : {val['macro_f1']:.4f}")
        print(f"    weighted F1 : {val['weighted_f1']:.4f}")
        print(f"    per-class:")
        for cls, m in val["per_class"].items():
            print(f"      {cls:<14} P={m['precision']:.3f}  "
                  f"R={m['recall']:.3f}  F1={m['f1']:.3f}  n={m['support']}")
        print(f"    confusion matrix (rows=true, cols=pred, "
              f"order={val['confusion_labels']}):")
        for row, lbl in zip(val["confusion_matrix"], val["confusion_labels"]):
            row_str = "  ".join(f"{v:>4d}" for v in row)
            print(f"      {lbl:<14} {row_str}")

    def _frames_to_xy(self, frames: list, label: int) -> tuple:
        """Converte lista frame in feature matrix X, label vector y."""
        X, y = [], []
        window = deque(maxlen=self.window_frames)

        for f in frames:
            window.append(f)
            if len(window) == self.window_frames:
                vec = csi_window_to_vector(list(window))
                if vec is not None:
                    X.append(vec)
                    y.append(label)

        return X, y

    def _frames_to_xy_custom(self, frames: list, label: int,
                              known_sources: list[str],
                              feature_names: list[str],
                              source_key: str = "mac") -> tuple:
        """Converte lista frame in feature matrix per modello per-sorgente."""
        X, y = [], []
        window = deque(maxlen=self.window_frames)

        for f in frames:
            window.append(f)
            if len(window) == self.window_frames:
                vec = csi_window_to_vector_per_source(
                    list(window), known_sources, feature_names, source_key
                )
                if vec is not None:
                    X.append(vec)
                    y.append(label)

        return X, y

    def train(
        self,
        empty_frames: list,
        stationary_frames: list | None = None,
        movement_frames: list | None = None,
    ) -> dict:
        """Addestra il Random Forest multi-classe.

        Args:
            empty_frames: frame CSI stanza vuota (classe EMPTY)
            stationary_frames: frame CSI persona ferma (classe STILL, opzionale)
            movement_frames: frame CSI con movimento (classe MOTION)

        Returns:
            dict con metriche di training.
        """
        self._check_sklearn()
        assert _RF_CLASS is not None  # type narrowing

        if not empty_frames or not movement_frames:
            raise ValueError("Servono almeno frame EMPTY e MOTION")

        X0, y0 = self._frames_to_xy(empty_frames, EMPTY)
        X1, y1 = ([], [])
        if stationary_frames:
            X1, y1 = self._frames_to_xy(stationary_frames, STILL)
        X2, y2 = self._frames_to_xy(movement_frames, MOTION)

        if len(X0) < 3:
            raise ValueError(f"Pochi feature vector da EMPTY: {len(X0)}")
        if len(X2) < 3:
            raise ValueError(f"Pochi feature vector da MOTION: {len(X2)}")

        X = X0 + X1 + X2
        y = y0 + y1 + y2

        n_classes = len(set(y))
        print(f"  [CSIClassifier] Training: {len(X)} campioni x {len(X[0])} feature")
        print(f"  Classi: EMPTY={y0.count(EMPTY)}, "
              f"STILL={y1.count(STILL)}, "
              f"MOTION={y2.count(MOTION)}")

        rf_kwargs: dict[str, Any] = {
            "n_estimators": 50,
            "max_depth": 8,
            "class_weight": "balanced",
            "random_state": 42,
            "n_jobs": 1,
            "oob_score": True,
            "bootstrap": True,
        }

        # Holdout validation 80/20 stratified PRIMA del fit di produzione,
        # così le metriche riportate non sono in-sample.
        val_metrics = self._holdout_validate(X, y, CSI_CLASSES, rf_kwargs)
        self._print_validation(val_metrics)

        # Modello di produzione: refit su tutto il dataset (le metriche di
        # generalizzazione restano quelle del val_metrics sopra).
        self._model = _RF_CLASS(**rf_kwargs)
        self._model.fit(X, y)
        self._trained = True

        oob = None
        if hasattr(self._model, "oob_score_") and self._model.oob_score_ is not None:
            try:
                oob = round(float(self._model.oob_score_), 4)
            except (TypeError, ValueError):
                oob = None
            if oob is not None:
                print(f"\n  OOB score (production model): {oob:.4f}")

        # Feature importance
        self._feature_importance = dict(
            sorted(
                (
                    (n, round(v, 4))
                    for n, v in zip(CSI_FEATURE_NAMES, self._model.feature_importances_)
                ),
                key=lambda kv: -kv[1],
            )
        )

        metrics = {
            "n_train": len(X),
            "n_features": len(X[0]),
            "n_classes": n_classes,
            "class_distribution": {
                CSI_CLASSES[c]: y.count(c) for c in sorted(set(y))
            },
            "feature_importance": dict(
                list(self._feature_importance.items())[:15]
            ),
            "validation": val_metrics,
            "oob_score": oob,
        }

        print(f"\n  Top-15 feature importance:")
        for name, imp in list(self._feature_importance.items())[:15]:
            bar = "█" * max(1, int(imp * 60))
            print(f"    {name:>28}: {imp:.4f}  {bar}")

        return metrics

    def train_custom(self, labeled_frames: dict[str, list]) -> dict:
        """Addestra con etichette personalizzate (es. posizioni).

        Usa feature multi-MAC + fase se più trasmettitori rilevati.

        Args:
            labeled_frames: dict {label_str: [frame_dict, ...]}

        Returns:
            dict con metriche di training.
        """
        self._check_sklearn()
        assert _RF_CLASS is not None

        if len(labeled_frames) < 2:
            raise ValueError("Servono almeno 2 classi")

        self._class_labels = list(labeled_frames.keys())

        # Scansiona tutti i frame per trovare sorgenti uniche
        # Priorità: source_id > mac > nessuna
        all_sources: set = set()
        source_key = "source_id"  # default se troviamo source_id nei frame
        for frames in labeled_frames.values():
            for f in frames:
                sid = f.get("source_id")
                if sid is not None:
                    all_sources.add(str(sid))
                elif f.get("mac") and isinstance(f.get("mac"), str):
                    all_sources.add(f.get("mac"))
        if not all_sources:
            source_key = "mac"  # nessuna sorgente → standard extraction
            all_sources = set()

        # Se source_id presente in alcuni frame ma non in altri, usa mac
        has_source_id = any(
            f.get("source_id") is not None
            for frames in labeled_frames.values()
            for f in frames
        )
        if has_source_id:
            source_key = "source_id"
            all_sources = set()
            for frames in labeled_frames.values():
                for f in frames:
                    sid = f.get("source_id")
                    if sid is not None:
                        all_sources.add(str(sid))
        else:
            macs = set()
            for frames in labeled_frames.values():
                for f in frames:
                    m = f.get("mac")
                    if m and isinstance(m, str):
                        macs.add(m)
            if macs:
                source_key = "mac"
                all_sources = macs

        self._source_key = source_key
        self._known_sources = sorted(all_sources)

        use_per_source = len(self._known_sources) >= 2
        if use_per_source:
            self._custom_feature_names = _generate_source_feature_names(
                self._known_sources, source_key
            )
            label_str = ", ".join(
                _prefix_from_value(s, source_key) for s in self._known_sources
            )
            print(f"  [Posizioni] Rilevate {len(self._known_sources)} sorgenti ({source_key}): {label_str}")
        else:
            self._known_sources = []
            self._custom_feature_names = []
            print(f"  [Posizioni] Sorgente singola, uso feature standard")

        X, y = [], []
        class_counts = {}
        for label_idx, (label_name, frames) in enumerate(labeled_frames.items()):
            if not frames:
                continue
            if use_per_source:
                Xi, yi = self._frames_to_xy_custom(
                    frames, label_idx,
                    self._known_sources,
                    self._custom_feature_names,
                    source_key,
                )
            else:
                Xi, yi = self._frames_to_xy(frames, label_idx)
            X.extend(Xi)
            y.extend(yi)
            class_counts[label_name] = len(yi)

        if len(X) < 10:
            raise ValueError(f"Troppi pochi campioni: {len(X)} (servono almeno 10)")

        n_features = len(X[0])
        feature_names = self._custom_feature_names if use_per_source else CSI_FEATURE_NAMES
        print(f"  [Posizioni] Training: {len(X)} campioni x {n_features} feature")
        for name, count in class_counts.items():
            print(f"    {name}: {count}")

        rf_kwargs: dict[str, Any] = {
            "n_estimators": 50,
            "max_depth": 8,
            "class_weight": "balanced",
            "random_state": 42,
            "n_jobs": 1,
            "oob_score": True,
            "bootstrap": True,
        }

        # Per train_custom le label sono indici interi → nome via self._class_labels.
        # Holdout validation 80/20 stratified PRIMA del fit di produzione.
        val_metrics = self._holdout_validate(X, y, self._class_labels, rf_kwargs)
        self._print_validation(val_metrics)

        # NB: per task di posizione su griglia, la validazione spaziale corretta
        # è leave-one-cell-out (vedi PositionRegressor.cross_validate_loo_cell).
        # L'holdout random qui sopra può dare metriche ottimistiche se finestre
        # vicine nel tempo finiscono sia in train che in test.
        if any("r" in lbl and "c" in lbl for lbl in self._class_labels):
            print(f"  [Posizioni] AVVISO: per label di griglia (rXcY) preferisci "
                  f"PositionRegressor.cross_validate_loo_cell — l'holdout random "
                  f"sopra è troppo ottimistico per la generalizzazione spaziale.")

        # Modello di produzione: refit su tutto il dataset.
        self._model = _RF_CLASS(**rf_kwargs)
        self._model.fit(X, y)
        self._trained = True

        oob = None
        if hasattr(self._model, "oob_score_") and self._model.oob_score_ is not None:
            try:
                oob = round(float(self._model.oob_score_), 4)
            except (TypeError, ValueError):
                oob = None
            if oob is not None:
                print(f"\n  OOB score (production model): {oob:.4f}")

        self._feature_importance = dict(
            sorted(
                (
                    (n, round(v, 4))
                    for n, v in zip(feature_names, self._model.feature_importances_)
                ),
                key=lambda kv: -kv[1],
            )
        )

        metrics = {
            "n_train": len(X),
            "n_features": n_features,
            "n_classes": len(labeled_frames),
            "class_distribution": class_counts,
            "feature_importance": dict(
                list(self._feature_importance.items())[:15]
            ),
            "validation": val_metrics,
            "oob_score": oob,
        }

        print(f"\n  Top-15 feature importance:")
        for name, imp in list(self._feature_importance.items())[:15]:
            bar = "█" * max(1, int(imp * 60))
            print(f"    {name:>28}: {imp:.4f}  {bar}")

        return metrics

    # ---- Persistence ----

    def save(self, path: str = CSI_MODEL_PATH) -> str:
        """Salva modello con joblib."""
        if not self._trained:
            raise RuntimeError("Modello non addestrato, chiama train() prima")
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")

        _joblib.dump(self._model, path)
        print(f"  [CSIClassifier] Modello salvato: {path}")
        return path

    def load(self, path: str = CSI_MODEL_PATH) -> bool:
        """Carica modello salvato."""
        self._check_sklearn()
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")
        if not os.path.exists(path):
            print(f"  [CSIClassifier] Modello non trovato: {path}")
            return False

        self._model = _joblib.load(path)
        self._trained = True
        print(f"  [CSIClassifier] Modello caricato: {path}")
        return True

    def save_custom(self, path: str = POSITIONS_MODEL_PATH,
                    labels_path: str = POSITIONS_LABELS_PATH) -> str:
        """Salva modello posizioni + etichette + sorgenti note."""
        if not self._trained or self._class_labels is None:
            raise RuntimeError("Modello non addestrato con train_custom()")
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")

        _joblib.dump(self._model, path)
        n_feat_names = len(self._custom_feature_names) if self._custom_feature_names else 0
        meta = {
            "class_labels": self._class_labels,
            "known_sources": self._known_sources,
            "source_key": self._source_key,
            "n_features": self._model.n_features_in_ if hasattr(self._model, 'n_features_in_') else n_feat_names,
        }
        with open(labels_path, "w") as f:
            json.dump(meta, f)
        print(f"  [Posizioni] Modello salvato: {path}")
        print(f"  [Posizioni] Etichette: {self._class_labels}")
        if self._known_sources:
            print(f"  [Posizioni] Sorgenti ({self._source_key}): "
                  f"{[_prefix_from_value(s, self._source_key) for s in self._known_sources]}")
        return path

    def load_custom(self, path: str = POSITIONS_MODEL_PATH,
                   labels_path: str = POSITIONS_LABELS_PATH) -> bool:
        """Carica modello posizioni + etichette + sorgenti note."""
        self._check_sklearn()
        if _joblib is None:
            raise RuntimeError("joblib non installato (pip install joblib)")
        if not os.path.exists(path) or not os.path.exists(labels_path):
            return False

        self._model = _joblib.load(path)
        with open(labels_path) as f:
            data = json.load(f)

        # Supporta formato vecchio (solo lista), nuovo (dict), e intermedio (known_macs)
        if isinstance(data, list):
            self._class_labels = data
            self._known_sources = []
            self._source_key = "mac"
        else:
            self._class_labels = data.get("class_labels", [])
            # Retrocompat: "known_macs" → "known_sources"
            self._known_sources = data.get("known_sources", data.get("known_macs", []))
            self._source_key = data.get("source_key", "mac")

        if self._known_sources and len(self._known_sources) >= 2:
            # Genera feature names; se non matchano n_features del modello,
            # riprova senza RSSI (retrocompat modelli salvati prima delle RSSI feature).
            candidate_names = _generate_source_feature_names(
                self._known_sources, self._source_key
            )
            expected_n = len(candidate_names)
            model_n = self._model.n_features_in_ if hasattr(self._model, 'n_features_in_') else expected_n

            if expected_n == model_n:
                self._custom_feature_names = candidate_names
            else:
                # Retrocompat: modello vecchio (senza RSSI features)
                _old_source_feature_names = lambda vals, sk: (
                    list(GLOBAL_FEATURE_NAMES) + [
                        f"{_prefix_from_value(v, sk)}_{pfx}{i}"
                        for v in vals
                        for pfx in [SUB_MEAN_PREFIX, SUB_STD_PREFIX,
                                    SUB_PHASE_MEAN_PREFIX, SUB_PHASE_STD_PREFIX]
                        for i in range(NUM_CSI_SUBCARRIERS)
                    ]
                )
                old_names = _old_source_feature_names(self._known_sources, self._source_key)
                if len(old_names) == model_n:
                    self._custom_feature_names = old_names
                    print(f"  [Posizioni] Feature names retrocompat (senza RSSI): {len(old_names)} feat")
                else:
                    self._custom_feature_names = candidate_names
        else:
            self._custom_feature_names = []

        self._trained = True
        print(f"  [Posizioni] Modello caricato: {path}")
        print(f"  [Posizioni] Etichette: {self._class_labels}")
        if self._known_sources:
            print(f"  [Posizioni] Sorgenti ({self._source_key}): "
                  f"{[_prefix_from_value(s, self._source_key) for s in self._known_sources]}")
        return True

    # ---- Real-time inference ----

    def add_frame(self, frame: dict) -> None:
        """Aggiunge un frame CSI per inferenza."""
        self.frame_hist.append(frame)
        if self._t0 == 0:
            self._t0 = time.time()

    def predict_proba(self) -> dict:
        """Probabilità per ogni classe.

        Returns:
            dict {label: prob} — classi standard o custom se train_custom() usato.
            Tutte a 0.0 se non pronto.
        """
        if not self.ready:
            return {lbl: 0.0 for lbl in (self._class_labels or CSI_LABELS)}
        assert self._model is not None  # garantito da self.ready

        # Usa feature per-sorgente se sorgenti multiple, altrimenti feature standard
        use_per_source = len(self._known_sources) >= 2
        if use_per_source:
            vec = csi_window_to_vector_per_source(
                list(self.frame_hist), self._known_sources,
                self._custom_feature_names, self._source_key,
            )
            feature_domain = self._custom_feature_names
        else:
            vec = csi_window_to_vector(list(self.frame_hist))
            feature_domain = CSI_FEATURE_NAMES

        if vec is None:
            return {lbl: 0.0 for lbl in (self._class_labels or CSI_LABELS)}

        probas = self._model.predict_proba([vec])[0]

        # Mappa probabilità alle classi
        result = {}
        labels = self._class_labels or CSI_LABELS
        for i, cls in enumerate(self._model.classes_):
            if self._class_labels:
                result[self._class_labels[int(cls)]] = round(float(probas[i]), 4)
            else:
                result[CSI_CLASSES[int(cls)]] = round(float(probas[i]), 4)

        # Assicura che tutte le classi siano presenti
        for lbl in labels:
            if lbl not in result:
                result[lbl] = 0.0

        self._last_probas = result
        self._last_features = {
            k: v for k, v in zip(feature_domain, vec)
        }
        return result

    def predict(self) -> str:
        """Classe predetta."""
        if not self.ready:
            return "UNKNOWN"
        assert self._model is not None  # garantito da self.ready

        use_per_source = len(self._known_sources) >= 2
        if use_per_source:
            vec = csi_window_to_vector_per_source(
                list(self.frame_hist), self._known_sources,
                self._custom_feature_names, self._source_key,
            )
        else:
            vec = csi_window_to_vector(list(self.frame_hist))

        if vec is None:
            return "UNKNOWN"
        cls = int(self._model.predict([vec])[0])
        if self._class_labels:
            return self._class_labels[cls]
        return CSI_CLASSES.get(cls, "UNKNOWN")

    def get_features(self) -> dict:
        """Ultime feature estratte (per debug)."""
        return dict(self._last_features)

    def get_info(self) -> dict:
        return {
            "trained": self._trained,
            "window_frames": self.window_frames,
            "buffer_fill": len(self.frame_hist),
            "ready": self.ready,
            "last_probas": dict(self._last_probas),
            "feature_importance": dict(
                list(self._feature_importance.items())[:10]
            ),
        }


def main():
    """CLI minima per training offline."""
    import argparse

    parser = argparse.ArgumentParser(description="CSI ML Classifier — training e test")
    parser.add_argument("--train", nargs="+", metavar=("EMPTY.json", "[STILL.json]", "MOTION.json"),
                        help="File JSON con frame etichettati (min 2: EMPTY MOTION)")
    parser.add_argument("--save", default=CSI_MODEL_PATH,
                        help="Percorso modello (default: csi_model.joblib)")
    parser.add_argument("--load", type=str, help="Carica modello e mostra info")
    args = parser.parse_args()

    if args.train:
        if len(args.train) < 2:
            print("ERRORE: servono almeno EMPTY.json e MOTION.json")
            sys.exit(1)

        labeled = {}
        for path in args.train:
            if not os.path.exists(path):
                print(f"ERRORE: file non trovato: {path}")
                sys.exit(1)
            with open(path) as f:
                data = json.load(f)
            frames = data if isinstance(data, list) else data.get("frames", data)
            # Estrai label dal nome file o dal contenuto
            label = "EMPTY"
            for kw in ("EMPTY", "STILL", "MOTION", "empty", "stationary", "movement"):
                if kw.lower() in os.path.basename(path).lower():
                    label = kw.upper()
                    break
            labeled[label] = frames
            print(f"  Caricati {len(frames)} frame da {path} → {label}")

        clf = CSIClassifier(window_frames=30)
        metrics = clf.train(
            labeled.get("EMPTY", []),
            labeled.get("STILL"),
            labeled.get("MOTION", []),
        )
        clf.save(args.save)

    elif args.load:
        if not os.path.exists(args.load):
            print(f"ERRORE: modello non trovato: {args.load}")
            sys.exit(1)
        clf = CSIClassifier()
        clf.load(args.load)
        info = clf.get_info()
        print(f"  Addestrato: {info['trained']}")
        print(f"  Finestra: {info['window_frames']} frame")
        print(f"  Feature importance top-10:")
        for name, imp in info["feature_importance"].items():
            print(f"    {name}: {imp:.4f}")

    else:
        parser.print_help()


