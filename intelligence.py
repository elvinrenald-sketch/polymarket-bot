import sqlite3
import pandas as pd
import numpy as np
import json
import os
import logging
from datetime import datetime
from typing import Optional, List, Dict
from joblib import dump, load
try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.exceptions import NotFittedError
except ImportError:
    RandomForestClassifier = None

log = logging.getLogger('poly.brain')

class TradingBrain:
    def __init__(self, db_path: str, model_path: str):
        self.db_path = db_path
        self.model_path = model_path
        self.model = None
        self.min_data_threshold = 30
        self.load_model()

    def load_model(self):
        if os.path.exists(self.model_path) and RandomForestClassifier:
            try:
                self.model = load(self.model_path)
                log.info(f"[BRAIN] Model loaded from {self.model_path}")
            except Exception as e:
                log.error(f"[BRAIN] Failed to load model: {e}")

    def save_model(self):
        if self.model:
            try:
                dump(self.model, self.model_path)
                log.info(f"[BRAIN] Model saved to {self.model_path}")
            except Exception as e:
                log.error(f"[BRAIN] Failed to save model: {e}")

    def extract_features_from_json(self, raw_json: str) -> Dict:
        """Converts stored JSON features into a flat dict for the model."""
        try:
            data = json.loads(raw_json)
            return {
                'liquidity': float(data.get('liquidity', 0)),
                'volume_24h': float(data.get('volume_24h', 0)),
                'days_left': float(data.get('days', 0)) if data.get('days') is not None else 7.0,
                'momentum_pct': float(data.get('momentum_pct', 0)),
                'spread_pct': float(data.get('spread_pct', 0)),
                'score': float(data.get('score', 0)),
                'entry_price': float(data.get('entry_price', 0.5)),
                'is_arb': 1 if data.get('is_arb') else 0,
                'category_score': self._cat_to_val(data.get('category', 'General'))
            }
        except:
            return {}

    def _cat_to_val(self, cat: str) -> int:
        mapping = {'Crypto': 1, 'Politics': 2, 'Sports': 3, 'Business': 4, 'Science': 5}
        return mapping.get(cat, 0)

    def train(self) -> bool:
        if not RandomForestClassifier:
            log.warning("[BRAIN] scikit-learn not available. Skipping training.")
            return False

        try:
            conn = sqlite3.connect(self.db_path)
            # Only train on CLOSED trades with valid results
            df = pd.read_sql_query(
                "SELECT features_json, result FROM positions WHERE status='CLOSED' AND result IN ('WIN', 'LOSS')",
                conn
            )
            conn.close()

            if len(df) < self.min_data_threshold:
                log.info(f"[BRAIN] Not enough data to train ({len(df)}/{self.min_data_threshold} closed trades)")
                return False

            # Transform JSON to Features Table
            feature_list = []
            for idx, row in df.iterrows():
                f = self.extract_features_from_json(row['features_json'])
                if f:
                    f['target'] = 1 if row['result'] == 'WIN' else 0
                    feature_list.append(f)

            if not feature_list:
                return False

            train_df = pd.DataFrame(feature_list)
            X = train_df.drop('target', axis=1)
            y = train_df['target']

            self.model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
            self.model.fit(X, y)
            
            self.save_model()
            log.info(f"[BRAIN] Training complete. Data points: {len(df)}")
            return True
        except Exception as e:
            log.error(f"[BRAIN] Training error: {e}")
            return False

    def predict_confidence(self, features: dict) -> float:
        """Returns 0-100 score. 100 means very high confidence."""
        if not self.model or not RandomForestClassifier:
            return 100.0 # Default to neutral/high if no model exists (Learning phase)

        try:
            # Flatten features into the same format used for training
            f_dict = {
                'liquidity': float(features.get('liquidity', 0)),
                'volume_24h': float(features.get('volume_24h', 0)),
                'days_left': float(features.get('days', 0)) if features.get('days') is not None else 7.0,
                'momentum_pct': float(features.get('momentum_pct', 0)),
                'spread_pct': float(features.get('spread_pct', 0)),
                'score': float(features.get('score', 0)),
                'entry_price': float(features.get('entry_price', 0.5)),
                'is_arb': 1 if features.get('is_arb') else 0,
                'category_score': self._cat_to_val(features.get('category', 'General'))
            }
            
            X_new = pd.DataFrame([f_dict])
            prob = self.model.predict_proba(X_new)[0][1] # Probability of Class 1 (WIN)
            return round(prob * 100, 1)
        except Exception as e:
            log.error(f"[BRAIN] Prediction error: {e}")
            return 100.0 # Fallback
