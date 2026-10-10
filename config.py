"""
config.py — Configuration centralisée du pipeline IA Marketing GA4
===================================================================

Modifiez ce fichier pour choisir le backend LLM et les paramètres
associés. Aucun autre fichier n'a besoin d'être modifié.

Backends disponibles :
    "openrouter" → OpenRouter (compatible OpenAI, clé OPENROUTER_KEY dans .env)
    "gemini"     → API Google Gemini (clé API_KEY dans .env)
    "ollama"     → modèle local via Ollama (100% confidentiel)
    "openai"     → API OpenAI directe (clé OPENAI_API_KEY dans .env)
    "anthropic"  → API Anthropic (clé ANTHROPIC_API_KEY dans .env)
"""

# Charge automatiquement les variables depuis .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv non installé : les variables d'env système sont utilisées

# ── Backend LLM ────────────────────────────────────────────────────────────────
# Options : "gemini" | "openrouter" | "ollama" | "openai" | "anthropic"
LLM_BACKEND: str = "openrouter"

# ── OpenRouter ──────────────────────────────────────────────────────────────
# Clé lue depuis OPENROUTER_KEY dans .env
# Modèles gratuits disponibles sur https://openrouter.ai/models?q=free
OPENROUTER_URL: str         = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL: str       = "nvidia/nemotron-3-ultra-550b-a55b:free"
OPENROUTER_TEMPERATURE: float = 0.4
OPENROUTER_MAX_TOKENS: int    = 100000

# ── Google Gemini 2.5 Flash (Free Tier) ───────────────────────────────────────
# Cle lue depuis API_KEY dans .env
# Quotas Free Tier : RPM=5 | TPM=250 000 | RPD=20
#
# Noms de modele acceptes par le SDK google-genai :
#   "gemini-flash-lite-latest"  <- fonctionne avec cette cle (teste)
#   "gemini-2.0-flash"          <- disponible apres reset quota quotidien
#   "gemini-2.0-flash-lite"     <- idem
#   "gemini-2.5-flash"          <- non disponible pour les nouveaux comptes
GEMINI_MODEL: str        = "gemini-flash-lite-latest"
GEMINI_TEMPERATURE: float = 0.4
GEMINI_MAX_TOKENS: int    = 8192   # max output tokens par appel

# ── Ollama (local) ─────────────────────────────────────────────────────────────
# Assurez-vous qu'Ollama est lancé et que le modèle est téléchargé :
#   ollama serve
#   ollama pull qwen2.5:7b
# Alternatives testées : "mistral:7b-instruct-q4_K_M", "phi3:mini"
OLLAMA_MODEL: str = "qwen2.5:7b"
OLLAMA_URL: str   = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT_S: int = 300     # secondes (les modèles locaux sont plus lents)
OLLAMA_TEMPERATURE: float = 0.4  # faible pour un plan structuré

# ── OpenAI ─────────────────────────────────────────────────────────────────────
# Mettre votre clé dans la variable d'environnement OPENAI_API_KEY
# (ne jamais la mettre en dur dans ce fichier)
OPENAI_MODEL: str       = "gpt-4o-mini"
OPENAI_TEMPERATURE: float = 0.4
OPENAI_MAX_TOKENS: int    = 4096

# ── Anthropic ──────────────────────────────────────────────────────────────────
# Mettre votre clé dans la variable d'environnement ANTHROPIC_API_KEY
ANTHROPIC_MODEL: str       = "claude-3-haiku-20240307"
ANTHROPIC_TEMPERATURE: float = 0.4
ANTHROPIC_MAX_TOKENS: int    = 4096

# ── Chemins des fichiers ────────────────────────────────────────────────────────
from pathlib import Path
ROOT: Path              = Path(__file__).parent
INSIGHTS_JSON: Path     = ROOT / "insights_consolides.json"
MARKETING_PLAN_MD: Path = ROOT / "plan_marketing.md"

# ── Langue du plan marketing ────────────────────────────────────────────────────
# "fr" pour français, "ar" pour arabe, "en" pour anglais
PLAN_LANGUAGE: str = "fr"
