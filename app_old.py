import os
import logging
from flask import Flask, render_template, request, redirect, url_for, jsonify
import pandas as pd

# --------------------------- Configuration ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # change this for production

# --------------------------- Constants ---------------------------
MAX_ANSWERS = 10  # maximum number of answers per question
REQUIRED_QUESTION_COUNT = 4  # number of questions to choose in a round

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

# --------------------------- Game State ---------------------------
class GameState:
    def __init__(self):
        self.all_questions = []
        self.chosen_questions = []
        self.current_question_index = -1
        self.team1_name = ""
        self.team2_name = ""
        self.team1_score = 0
        self.team2_score = 0
        self.faceoff_winner = None      # 1 or 2
        self.current_control_team = None  # 1 or 2
        self.is_steal_attempt = False
        self.answers_revealed = []
        self.team_in_play_strikes = 0
        self.current_question = None
        self.question_shown_to_contestants = False
        self.faceoff_done = False  # flag to ensure face-off is done only once per question

    def load_questions_from_excel(self, file_path: str) -> int:
        logging.info(f"Loading questions from {file_path}")
        df = pd.read_excel(file_path)
        # Verify that required columns exist
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
                        logging.warning(
                            f"Invalid points value at question {q_data['question_number']} answer {i}"
                        )
                        break
                else:
                    break
            self.all_questions.append(q_data)
        logging.info(f"Loaded {len(self.all_questions)} questions.")
        return len(self.all_questions)

    def set_round_questions_and_teams(self, chosen_indices, team1_name, team2_name):
        logging.info(f"Setting round with questions indices: {chosen_indices}")
        self.chosen_questions = [self.all_questions[i] for i in chosen_indices]
        self.team1_name = team1_name.strip()
        self.team2_name = team2_name.strip()
        # Reset scores and question state for a new round
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
        self.faceoff_winner = winner_team
        logging.info(f"Face-off winner set to Team {winner_team}")

    def apply_play_pass(self, play):
        if self.faceoff_winner is None:
            raise RuntimeError("Face-off winner not set")
        # If “play” then control remains with the face-off winner; if “pass” then control goes to the other team.
        self.current_control_team = self.faceoff_winner if play else (2 if self.faceoff_winner == 1 else 1)
        decision = "Play" if play else "Pass"
        logging.info(f"Applied {decision}. Control Team: {self.current_control_team}")

    def reveal_answer_by_index(self, index):
        if 0 <= index < len(self.answers_revealed):
            if not self.answers_revealed[index]:
                self.answers_revealed[index] = True
                logging.info(f"Revealed answer {index}: {self.current_question['answers'][index][0]}")

    def add_strike(self):
        self.team_in_play_strikes += 1
        logging.info(f"Added strike. Total: {self.team_in_play_strikes}")
        return self.team_in_play_strikes

    def enable_steal_attempt(self):
        self.is_steal_attempt = True
        logging.info("Steal attempt enabled.")

    def check_all_answers_revealed(self):
        return all(self.answers_revealed)

    def award_points(self, team_num):
        revealed_points = sum(
            pts for (ans, pts), rev in zip(self.current_question['answers'], self.answers_revealed) if rev
        )
        if team_num == 1:
            self.team1_score += revealed_points
        else:
            self.team2_score += revealed_points
        logging.info(f"Awarded {revealed_points} points to Team {team_num}")
        return revealed_points

    def steal_successful(self):
        stealing_team = 1 if self.current_control_team == 2 else 2
        logging.info(f"Steal successful by Team {stealing_team}")
        return stealing_team

    def scores(self):
        return self.team1_score, self.team2_score

    def set_scores(self, t1, t2):
        self.team1_score = t1
        self.team2_score = t2
        logging.info(f"Scores set: Team1 - {t1}, Team2 - {t2}")

# --------------------------- Global GameState Instance ---------------------------
game_state = GameState()
if os.path.exists("ff_questions.xlsx"):
    try:
        game_state.load_questions_from_excel("ff_questions.xlsx")
    except Exception as e:
        logging.error("Failed to load questions: " + str(e))
else:
    logging.error("ff_questions.xlsx not found.")

# --------------------------- Routes ---------------------------
@app.route('/')
def index():
    return redirect(url_for('moderator'))

@app.route('/moderator', methods=['GET'])
def moderator():
    return render_template('moderator.html', state=game_state)

@app.route('/contestant', methods=['GET'])
def contestant():
    return render_template('contestant.html', state=game_state)

@app.route('/api/state', methods=['GET'])
def api_state():
    data = {
        "question_shown": game_state.question_shown_to_contestants,
        "current_question": {
            "question": game_state.current_question['question'] if game_state.current_question else "",
            "answers": [
                {"text": ans[0], "points": ans[1], "revealed": rev}
                for (ans, rev) in zip(game_state.current_question['answers'], game_state.answers_revealed)
            ]
        } if game_state.current_question else None,
        "team1_score": game_state.team1_score,
        "team2_score": game_state.team2_score,
        "team1_name": game_state.team1_name,
        "team2_name": game_state.team2_name,
        "current_control_team": game_state.current_control_team,
        "strikes": game_state.team_in_play_strikes,
        "is_steal_attempt": game_state.is_steal_attempt,
        "current_question_index": game_state.current_question_index + 1,
        "total_questions": len(game_state.chosen_questions)
    }
    # If game is over, include winner info.
    if game_state.current_question is None and game_state.current_question_index >= len(game_state.chosen_questions):
        t1, t2 = game_state.scores()
        if t1 > t2:
            winner = {"name": game_state.team1_name, "points": t1}
        elif t2 > t1:
            winner = {"name": game_state.team2_name, "points": t2}
        else:
            winner = {"name": "Tie", "points": t1}
        data["winner"] = winner
    return jsonify(data)

@app.route('/round_setup', methods=['GET', 'POST'])
def round_setup():
    if request.method == 'POST':
        selected = request.form.getlist('questions')
        team1 = request.form.get('team1')
        team2 = request.form.get('team2')
        if len(selected) != REQUIRED_QUESTION_COUNT:
            error = f"Please select exactly {REQUIRED_QUESTION_COUNT} questions."
            return render_template('round_setup.html', error=error, questions=game_state.all_questions)
        if not team1 or not team2:
            error = "Please enter both team names."
            return render_template('round_setup.html', error=error, questions=game_state.all_questions)
        indices = [int(i) for i in selected]
        game_state.set_round_questions_and_teams(indices, team1, team2)
        return redirect(url_for('moderator'))
    return render_template('round_setup.html', questions=game_state.all_questions)

@app.route('/moderator/start', methods=['POST'])
def start_next_question():
    if not game_state.start_next_question():
        # No more questions; moderator page can show winner info via the API.
        return redirect(url_for('moderator'))
    return redirect(url_for('moderator'))

@app.route('/moderator/show_question', methods=['POST'])
def show_question():
    if game_state.current_question:
        game_state.question_shown_to_contestants = True
    return redirect(url_for('moderator'))

@app.route('/moderator/reveal/<int:index>', methods=['POST'])
def reveal_answer(index):
    if not game_state.question_shown_to_contestants:
        return "Question not shown yet.", 400
    if index < 0 or index >= len(game_state.current_question['answers']):
        return "Invalid index", 400
    if not game_state.answers_revealed[index]:
        game_state.reveal_answer_by_index(index)
        # Trigger face-off dialog if this is the first answer revealed
        if not game_state.faceoff_done:
            game_state.faceoff_done = True
            return redirect(url_for('faceoff'))
    return redirect(url_for('moderator'))

@app.route('/moderator/strike', methods=['POST'])
def strike():
    strikes = game_state.add_strike()
    if strikes >= 3:
        game_state.enable_steal_attempt()
    return redirect(url_for('moderator'))

@app.route('/moderator/steal', methods=['POST'])
def steal():
    # When a steal attempt fails, award points to the current control team and then switch control.
    game_state.is_steal_attempt = False
    if game_state.current_control_team is not None:
        game_state.award_points(game_state.current_control_team)
        game_state.current_control_team = 1 if game_state.current_control_team == 2 else 2
    return redirect(url_for('moderator'))

@app.route('/moderator/update_scores', methods=['POST'])
def update_scores():
    try:
        t1 = int(request.form.get('team1_score', game_state.team1_score))
        t2 = int(request.form.get('team2_score', game_state.team2_score))
        game_state.set_scores(t1, t2)
    except ValueError:
        pass
    return redirect(url_for('moderator'))

@app.route('/moderator/faceoff', methods=['GET', 'POST'])
def faceoff():
    if request.method == 'POST':
        winner = request.form.get('faceoff_winner')
        playpass = request.form.get('playpass')
        if not winner or not playpass:
            error = "Please select both a face-off winner and a play/pass decision."
            return render_template('faceoff.html', error=error, team1=game_state.team1_name, team2=game_state.team2_name)
        winner_team = int(winner)
        game_state.set_faceoff_winner(winner_team)
        play = True if playpass == "play" else False
        game_state.apply_play_pass(play)
        return redirect(url_for('moderator'))
    return render_template('faceoff.html', team1=game_state.team1_name, team2=game_state.team2_name)

if __name__ == '__main__':
    app.run(debug=True)
