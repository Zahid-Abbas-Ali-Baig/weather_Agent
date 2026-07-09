"""
Flask Web Interface for Weather + Outfit Advisor Agent

A clean, production-ready presentation layer that:
- Separates presentation from agent logic
- Uses proper session management
- Handles errors gracefully
- Provides RESTful API endpoints

Run: python app.py
Access: http://localhost:5000
"""

import uuid
import json
from flask import Flask, render_template, request, jsonify, session
from config import config
import weather_agent


def agent_result_to_json(result):
    """Normalize agent return values into a JSON-friendly response."""
    if isinstance(result, dict):
        if result.get("needs_confirmation"):
            return {
                "reply": result["reply"],
                "needs_confirmation": True,
            }
        if result.get("needs_disambiguation"):
            return {
                "reply": result["reply"],
                "needs_disambiguation": True,
                "options": result["options"],
            }
        if "reply" in result and "weather" in result:
            return {
                "reply": result["reply"],
                "weather": result["weather"],
            }
        return {"reply": result.get("reply", str(result))}
    return {"reply": result}


def create_app(config_name='default'):
    """Application factory pattern for clean initialization."""
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # In-memory session storage (use Redis in production)
    app.sessions = {}
    
    # Register routes
    register_routes(app)
    
    return app


def register_routes(app):
    """Register all routes in one place."""
    
    @app.route('/')
    def index():
        """Render main chat interface."""
        data = get_session(app)
        return render_template('index.html', 
                               remembered_city=data['memory'].get('last_city'))
    
    @app.route('/api/chat', methods=['POST'])
    def chat():
        """Handle chat messages - main agent endpoint."""
        user_message = request.json.get('message', '').strip()
        disambiguation_index = request.json.get('disambiguation_index')
        
        if not user_message and disambiguation_index is None:
            return jsonify({'error': 'Empty message'}), 400
        
        if len(user_message) > weather_agent.INPUT_MAX_LENGTH:
            return jsonify({
                'reply': f'Your message is too long. Please keep it under {weather_agent.INPUT_MAX_LENGTH} characters.'
            })
        
        data = get_session(app)
        
        try:
            pending_disambiguation = data.get('pending_disambiguation')
            if pending_disambiguation is not None and disambiguation_index is not None:
                reply = weather_agent.continue_after_disambiguation(
                    data['chat_history'],
                    data['memory'],
                    pending_disambiguation['original_message'],
                    pending_disambiguation['city_query'],
                    int(disambiguation_index),
                    pending_disambiguation['locations'],
                )
                data.pop('pending_disambiguation', None)
                return jsonify(agent_result_to_json(reply))

            pending = data.get('pending_confirmation')
            if pending:
                answer = user_message.strip().lower()
                if answer in ('yes', 'y', 'no', 'n'):
                    confirmed = answer in ('yes', 'y')
                    reply = weather_agent.continue_after_city_confirmation(
                        data['chat_history'],
                        data['memory'],
                        pending['original_message'],
                        pending['pending_city'],
                        confirmed,
                    )
                    data.pop('pending_confirmation', None)
                    return jsonify(agent_result_to_json(reply))

            result = weather_agent.run_turn(
                data['chat_history'],
                data['memory'],
                user_message
            )

            if isinstance(result, dict) and result.get('needs_confirmation'):
                data['pending_confirmation'] = {
                    'pending_city': result['pending_city'],
                    'original_message': result['original_message'],
                }

            if isinstance(result, dict) and result.get('needs_disambiguation'):
                data['pending_disambiguation'] = {
                    'city_query': result['city_query'],
                    'original_message': result['original_message'],
                    'locations': result['locations'],
                }

            return jsonify(agent_result_to_json(result))
        except Exception as e:
            app.logger.error(f"Chat error: {e}")
            return jsonify({'error': 'An error occurred. Please try again.'}), 500
    
    @app.route('/api/start', methods=['POST'])
    def start():
        """Initialize session with optional remembered city."""
        data = get_session(app)
        use_remembered = request.json.get('use_remembered', False)
        remembered_city = data['memory'].get('last_city')
        
        if remembered_city and use_remembered:
            if not weather_agent.is_valid_city(remembered_city):
                data['memory'].pop('last_city', None)
                weather_agent.save_memory(data['memory'])
                return jsonify({'reply': 'Invalid saved city. Where are you today?'})

            resolved = weather_agent.resolve_city(remembered_city)
            if 'error' in resolved:
                return jsonify({'reply': f"Sorry, couldn't find saved city: {resolved['error']}"})
            if resolved.get('needs_disambiguation'):
                data['pending_disambiguation'] = {
                    'city_query': remembered_city,
                    'original_message': f'[remembered city: {remembered_city}]',
                    'locations': resolved['locations'],
                }
                return jsonify(agent_result_to_json(resolved))

            weather = weather_agent.fetch_weather_for_location(resolved['location'])
            if 'error' in weather:
                return jsonify({'reply': f"Sorry, couldn't fetch weather: {weather['error']}"})
            
            data['chat_history'].append({
                'role': 'user',
                'content': f"[System note: current weather data -> {json.dumps(weather)}]"
            })
            reply = weather_agent.call_llm(data['chat_history'])
            data['chat_history'].append({'role': 'assistant', 'content': reply})
            return jsonify(agent_result_to_json({'reply': reply, 'weather': weather}))
        
        return jsonify({'reply': 'Where are you today?'})
    
    @app.route('/api/reset', methods=['POST'])
    def reset():
        """Clear session and start fresh."""
        session_id = session.get('session_id')
        if session_id and session_id in app.sessions:
            del app.sessions[session_id]
        session.clear()
        return jsonify({'status': 'ok'})
    
    @app.route('/health')
    def health():
        """Health check endpoint for deployment monitoring."""
        return jsonify({'status': 'healthy'})


def get_session(app):
    """Get or create session with chat history."""
    session_id = session.get('session_id')
    
    if not session_id or session_id not in app.sessions:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id
        app.sessions[session_id] = {
            'chat_history': [{
                'role': 'system', 
                'content': weather_agent.SYSTEM_PROMPT
            }],
            'memory': weather_agent.load_memory()
        }
    
    return app.sessions[session_id]


if __name__ == '__main__':
    import os
    env = os.getenv('FLASK_ENV', 'development')
    app = create_app(env)
    
    print("=== Weather + Outfit Advisor Web Interface ===")
    print(f"Environment: {env}")
    print(f"Access at: http://{app.config['HOST']}:{app.config['PORT']}")
    print("Press Ctrl+C to stop\n")
    
    app.run(
        host=app.config['HOST'],
        port=app.config['PORT'],
        debug=app.config['DEBUG']
    )