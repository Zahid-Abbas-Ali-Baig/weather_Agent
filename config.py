"""Configuration management for the Weather + Outfit Advisor Agent."""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Base configuration."""
    SECRET_KEY = os.getenv('SECRET_KEY', os.urandom(24).hex())
    
    # LLM Configuration
    LLM_BASE_URL = os.getenv('BASE_URL')
    LLM_MODEL = os.getenv('MODEL')
    LLM_API_KEY = os.getenv('API_KEY', 'not-needed')
    
    # Agent Configuration
    INPUT_MAX_LENGTH = int(os.getenv('INPUT_MAX_LENGTH', '500'))
    
    # Flask Configuration
    DEBUG = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    HOST = os.getenv('FLASK_HOST', '0.0.0.0')
    PORT = int(os.getenv('FLASK_PORT', '5000'))


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}