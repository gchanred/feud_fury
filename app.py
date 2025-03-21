import os
import logging
import uuid  # NEW: Import uuid to generate unique room IDs
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_socketio import SocketIO, join_room, emit
from flask import session

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)
app.secret_key = 'your_secret_key_here'
socketio = SocketIO(app)

MAX_ANSWERS = 10
REQUIRED_QUESTION_COUNT = 4
EXCEL_REQUIRED_COLUMNS = [
    'Question Number', 'Survey Question',
    'Answer 1', 'Answer 1 points',
    'Answer 2', 'Answer 2 points',
    'Answer 3', 'Answer 3 points',
    'Answer 4', 'Answer 4 points',
    'Answer 5', 'Answer 5 points',
    'Answer 6', 'Answer 6 points',
    'Answer 7', 'Answer 7 points',
    'Answer 8', 'Answer 8 points',
    'Answer 9', 'Answer 9 points',
    'Answer 10', 'Answer 10 points'
]

# Dictionary to hold a separate GameState per room.
room_game_states = {}

class GameState:
    def __init__(self):
        self.all_questions = []
        self.chosen_questions = []
        self.current_question_index = -1
        self.team1_name = ""
        self.team2_name = ""
        self.team1_score = 0
        self.team2_score = 0
        self.faceoff_winner = None
        self.current_control_team = None
        self.is_steal_attempt = False
        self.answers_revealed = []
        self.team_in_play_strikes = 0
        self.current_question = None
        self.question_shown_to_contestants = False
        self.faceoff_done = False

    def load_questions_from_excel(self, file_path: str) -> int:
        logging.info(f"Loading questions from {file_path}")
        df = pd.read_excel(file_path)
        for col in EXCEL_REQUIRED_COLUMNS:
            if col not in df.columns:
                error_msg = f"Missing required column: {col}"
                logging.error(error_msg)
                raise ValueError(error_msg)
        self.all_questions = []
        for _, row in df.iterrows():
            q_data = {
                'question_number': row.get('Question Number', None),
                'question': row.get('Survey Question', ''),
                'answers': []
            }
            for i in range(1, MAX_ANSWERS + 1):
                ans_col = f'Answer {i}'
                pts_col = f'Answer {i} points'
                ans_val = row.get(ans_col)
                pts_val = row.get(pts_col)
                if pd.notna(ans_val) and pd.notna(pts_val):
                    try:
                        q_data['answers'].append((str(ans_val), int(pts_val)))
                    except ValueError:
                        logging.warning(f"Invalid points value for question {q_data['question_number']} answer {i}")
                        break
                else:
                    break
            self.all_questions.append(q_data)
        logging.info(f"Loaded {len(self.all_questions)} questions.")
        return len(self.all_questions)

    def set_round_questions_and_teams(self, chosen_indices, team1_name, team2_name):
        logging.info(f"Setting round with question indices: {chosen_indices}")
        self.chosen_questions = [self.all_questions[int(i)] for i in chosen_indices]
        self.team1_name = team1_name.strip()
        self.team2_name = team2_name.strip()
        self.team1_score = 0
        self.team2_score = 0
        self.current_question_index = -1
        self.current_question = None
        self.faceoff_done = False

    def start_next_question(self):
        self.current_question_index += 1
        if self.current_question_index >= len(self.chosen_questions):
            logging.info("No more questions left.")
            self.current_question = None
            return False
        self.current_question = self.chosen_questions[self.current_question_index]
        self.answers_revealed = [False] * len(self.current_question['answers'])
        self.faceoff_winner = None
        self.current_control_team = None
        self.is_steal_attempt = False
        self.team_in_play_strikes = 0
        self.question_shown_to_contestants = False
        self.faceoff_done = False
        logging.info(f"Starting question: {self.current_question['question']}")
        return True

    def get_current_question_text(self):
        return self.current_question['question'] if self.current_question else ""

    def get_team_name(self, team_num):
        return self.team1_name if team_num == 1 else self.team2_name

    def set_faceoff_winner(self, winner_team):
        self.faceoff_winner = int(winner_team)
        logging.info(f"Face-off winner set to Team {self.faceoff_winner}")

    def apply_play_pass(self, play):
        if self.faceoff_winner is None:
            raise RuntimeError("Face-off winner not set")
        self.current_control_team = self.faceoff_winner if play else (2 if self.faceoff_winner == 1 else 1)
        logging.info(f"Play/Pass applied. Current control team: {self.current_control_team}")

    def reveal_answer_by_index(self, index):
        if index < 0 or index >= len(self.answers_revealed):
            logging.error("Invalid index in reveal_answer_by_index")
            return
        if not self.answers_revealed[index]:
            self.answers_revealed[index] = True
            logging.info(f"Answer revealed at index {index}: {self.current_question['answers'][index][0]}")

    def add_strike(self):
        self.team_in_play_strikes += 1
        logging.info(f"Strike added. Total strikes: {self.team_in_play_strikes}")
        return self.team_in_play_strikes

    def enable_steal_attempt(self):
        self.is_steal_attempt = True
        logging.info("Steal attempt enabled.")

    def check_all_answers_revealed(self):
        return all(self.answers_revealed)

    def award_points(self, team_num):
        total = 0
        for (answer, pts), revealed in zip(self.current_question['answers'], self.answers_revealed):
            if revealed:
                total += pts
        if team_num == 1:
            self.team1_score += total
        else:
            self.team2_score += total
        logging.info(f"Awarded {total} points to Team {team_num}")
        return total

    def steal_successful(self):
        stealing_team = 1 if self.current_control_team == 2 else 2
        logging.info(f"Steal successful. Team {stealing_team} gets points.")
        return stealing_team

    def scores(self):
        return self.team1_score, self.team2_score

    def set_scores(self, t1, t2):
        self.team1_score = t1
        self.team2_score = t2
        logging.info(f"Scores manually set: Team1 - {t1}, Team2 - {t2}")

# NEW: Function to generate a new room ID automatically.
def generate_room_id():
    return str(uuid.uuid4())

def get_game_state_for_room(room_id):
    if room_id not in room_game_states:
        room_game_states[room_id] = GameState()
        if os.path.exists("ff_questions.xlsx"):
            try:
                room_game_states[room_id].load_questions_from_excel("ff_questions.xlsx")
            except Exception as e:
                logging.error("Error loading questions: " + str(e))
        else:
            logging.error("ff_questions.xlsx not found.")
    return room_game_states[room_id]

def broadcast_state(room):
    state = get_game_state_for_room(room)
    data = {
        "question_shown": state.question_shown_to_contestants,
        "current_question": {
            "question": state.current_question['question'] if state.current_question and state.question_shown_to_contestants else "",
            "answers": [
                {"text": ans[0], "points": ans[1], "revealed": rev}
                for ans, rev in zip(state.current_question['answers'], state.answers_revealed)
            ]
        } if state.current_question else None,
        "team1_score": state.team1_score,
        "team2_score": state.team2_score,
        "team1_name": state.team1_name,
        "team2_name": state.team2_name,
        "current_control_team": state.current_control_team,
        "strikes": state.team_in_play_strikes,
        "is_steal_attempt": state.is_steal_attempt,
        "current_question_index": state.current_question_index + 1,
        "total_questions": len(state.chosen_questions)
    }
    socketio.emit('state_update', data, room=room)

@socketio.on('join')
def on_join(data):
    room = data.get('room', 'default')
    join_room(room)
    logging.info(f"Client joined room: {room}")
    broadcast_state(room)

# Updated index route to generate a new room automatically if none is provided.
@app.route('/')
def index():
    if 'room' not in session:
        session['room'] = generate_room_id()
    room = session['room']
    return redirect(url_for('moderator', room=room))

@app.route('/moderator')
def moderator():
    # room = request.args.get('room', 'default')
    room = session.get('room', 'default')
    state = get_game_state_for_room(room)
    return render_template('moderator.html', state=state, room=room)

@app.route('/contestant')
def contestant():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    return render_template('contestant.html', state=state, room=room)

@app.route('/instructions')
def instructions():
    room = request.args.get('room', 'default')
    return render_template('instructions.html', room=room)

@app.route('/api/state')
def api_state():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    data = {
        "question_shown": state.question_shown_to_contestants,
        "current_question": {
            "question": state.current_question['question'] if state.current_question and state.question_shown_to_contestants else "",
            "answers": [
                {"text": ans[0], "points": ans[1], "revealed": rev}
                for ans, rev in zip(state.current_question['answers'], state.answers_revealed)
            ]
        } if state.current_question else None,
        "team1_score": state.team1_score,
        "team2_score": state.team2_score,
        "team1_name": state.team1_name,
        "team2_name": state.team2_name,
        "current_control_team": state.current_control_team,
        "strikes": state.team_in_play_strikes,
        "is_steal_attempt": state.is_steal_attempt,
        "current_question_index": state.current_question_index + 1,
        "total_questions": len(state.chosen_questions)
    }
    if state.current_question is None and state.current_question_index >= len(state.chosen_questions):
        t1, t2 = state.scores()
        if t1 > t2:
            winner = {"name": state.team1_name, "points": t1}
        elif t2 > t1:
            winner = {"name": state.team2_name, "points": t2}
        else:
            winner = {"name": "Tie", "points": t1}
        data["winner"] = winner
    return jsonify(data)

@app.route('/round_setup', methods=['GET', 'POST'])
def round_setup():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if request.method == 'POST':
        selected = request.form.getlist('questions')
        team1 = request.form.get('team1')
        team2 = request.form.get('team2')
        if len(selected) != REQUIRED_QUESTION_COUNT:
            error = f"Please select exactly {REQUIRED_QUESTION_COUNT} questions."
            return render_template('round_setup.html', error=error, questions=state.all_questions, room=room)
        if not team1 or not team2:
            error = "Please provide both team names."
            return render_template('round_setup.html', error=error, questions=state.all_questions, room=room)
        state.set_round_questions_and_teams(selected, team1, team2)
        flash("Round setup complete.", "success")
        broadcast_state(room)
        return redirect(url_for('moderator', room=room))
    return render_template('round_setup.html', questions=state.all_questions, room=room)

@app.route('/moderator/start', methods=['POST'])
def start_next_question():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if not state.start_next_question():
        flash("No more questions available.", "info")
    else:
        flash("Next question started.", "success")
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/show_question', methods=['POST'])
def show_question():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if state.current_question:
        state.question_shown_to_contestants = True
        flash("Question shown to contestants.", "success")
    else:
        flash("No current question.", "error")
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/reveal/<int:index>', methods=['POST'])
def reveal_answer(index):
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if not state.question_shown_to_contestants:
        flash("You must show the question first.", "error")
        broadcast_state(room)
        return redirect(url_for('moderator', room=room))
    if state.current_question is None or index < 0 or index >= len(state.current_question['answers']):
        flash("Invalid answer index.", "error")
        broadcast_state(room)
        return redirect(url_for('moderator', room=room))
    if not state.answers_revealed[index]:
        state.reveal_answer_by_index(index)
        if not state.faceoff_done:
            state.faceoff_done = True
            broadcast_state(room)
            return redirect(url_for('faceoff', room=room))
        if state.check_all_answers_revealed():
            if state.current_control_team is not None:
                pts = state.award_points(state.current_control_team)
                flash(f"All answers revealed. {state.get_team_name(state.current_control_team)} awarded {pts} points.", "success")
                state.current_control_team = 1 if state.current_control_team == 2 else 2
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/strike', methods=['POST'])
def strike():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    strikes = state.add_strike()
    flash(f"Strike added. Total strikes: {strikes}", "info")
    if strikes >= 3:
        state.enable_steal_attempt()
        flash("Three strikes reached! Steal attempt enabled.", "warning")
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/steal_success', methods=['POST'])
def steal_success():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if state.current_control_team is not None:
        stealing_team = 1 if state.current_control_team == 2 else 2
        pts = state.award_points(stealing_team)
        flash(f"Steal successful. {state.get_team_name(stealing_team)} awarded {pts} points.", "success")
        state.current_control_team = 1 if state.current_control_team == 2 else 2
    state.is_steal_attempt = False
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/steal_failed', methods=['POST'])
def steal_failed():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if state.current_control_team is not None:
        pts = state.award_points(state.current_control_team)
        flash(f"Steal failed. {state.get_team_name(state.current_control_team)} awarded {pts} points.", "info")
        state.current_control_team = 1 if state.current_control_team == 2 else 2
    state.is_steal_attempt = False
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/update_scores', methods=['POST'])
def update_scores():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    try:
        t1 = int(request.form.get('team1_score', state.team1_score))
        t2 = int(request.form.get('team2_score', state.team2_score))
        state.set_scores(t1, t2)
        flash("Scores updated.", "success")
    except ValueError:
        flash("Invalid score values.", "error")
    broadcast_state(room)
    return redirect(url_for('moderator', room=room))

@app.route('/moderator/faceoff', methods=['GET', 'POST'])
def faceoff():
    room = request.args.get('room', 'default')
    state = get_game_state_for_room(room)
    if request.method == 'POST':
        winner = request.form.get('faceoff_winner')
        playpass = request.form.get('playpass')
        if not winner or not playpass:
            error = "Select both a face-off winner and a play/pass decision."
            broadcast_state(room)
            return render_template('faceoff.html', error=error, team1=state.team1_name, team2=state.team2_name, room=room)
        state.set_faceoff_winner(winner)
        play = True if playpass == "play" else False
        state.apply_play_pass(play)
        flash(f"Face-off complete. {state.get_team_name(state.current_control_team)} will play.", "success")
        broadcast_state(room)
        return redirect(url_for('moderator', room=room))
    return render_template('faceoff.html', team1=state.team1_name, team2=state.team2_name, room=room)

if __name__ == '__main__':
    socketio.run(app, debug=False)
