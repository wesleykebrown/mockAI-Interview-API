from .extensions import db
from flask import request, jsonify, session
import requests
from sqlalchemy import desc
from sqlalchemy.exc import SQLAlchemyError
import os
import io
import traceback
import json
import random
from datetime import datetime
import logging
import vercel_blob
from deepgram import DeepgramClient, PrerecordedOptions, FileSource
from flask_cors import CORS
from dotenv import load_dotenv
import google.generativeai as genai
from api.genai_utils import text_prompt_for_question, prompt_with_audio_file
from api.audio_analysis import analyze_audio, extract_score_from_gemini_response
from api.models import User, Question, Result
from api.genai_utils import prompt_with_audio_file, extract_analysis_results, sanitize_question


# https://github.com/orgs/vercel/discussions/6837
# Reference for imports in serverless

from . import app


# Initialize CORS
CORS(app)
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.ERROR)

# Initialize Deepgram client
deepgram = DeepgramClient(os.getenv("DG_API_KEY"))

IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"


def generate_interview_prompt(name, company, position, interview_type):
    if interview_type.lower() == "behavioral":
        prompt = (
            "You are an interviewer for a website called 'mockAI'. You are interviewing {name} "
            "for the position of {position} at {company}. The interview type is a {interview_type} interview. "
            "Ask a behavioral question that focuses on {name}'s experience, how they manage teamwork, leadership, conflict resolution, "
            "and communication skills in professional settings. Avoid generic questions like 'what would you do in a difficult situation?'. "
            "The question should be specific to scenarios they are likely to encounter in the {position} role. Ask {name} to answer the question "
            "within 3 minutes. DO NOT include any markdown in your response. Address them by their name."
        )
    else:
        prompt = (
            "You are an interviewer for a website called 'mockAI'. You are interviewing {name} "
            "for the position of {position} at {company}. The interview type is a {interview_type} interview. "
            "Ask a {interview_type} question to {name}. The goal of this question is to understand how {name} handles a situation "
            "relevant to the role of {position}. Ask {name} to answer the question within 3 minutes. "
            "DO NOT include any markdown in your response. Address them by their name."
        )

    return prompt.format(name=name, company=company, position=position, interview_type=interview_type)


def generate_feedback_prompt(name, company, position, interview_type, question):
    return (
        "You are an interview feedback analyst for a website called 'MockAI'. {name} has applied for the position of {position} at {company}. "
        "Job seekers submit their {interview_type} responses to questions, and your job is to help them improve. "
        "However, remember that many job seekers experience interview anxiety, so keep the feedback constructive and not too harsh. "
        "Give brief feedback where {name} can improve based on this {interview_type} response to the question: '{question}'. "
        "Provide a more efficient solution or correct approach to the question. "
        "Provide an unbiased score on a scale of 60 to 100 based on the technical efficiency of the response, in this exact format: 'Here is your score based on our assessment: [score]'. "
        "Afterward, thank {name} for their response, sign your name as 'MockAI', and encourage {name} to return to MockAI to keep practicing their interview skills. "
        "DO NOT include any markdown in your response."
    ).format(name=name, company=company, position=position, interview_type=interview_type, question=question)


@app.route("/service/upload_audio", methods=["POST"])
def upload_audio():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400
    elif "user" not in request.form or "question" not in request.form:
        return jsonify({"error": "User and question are required"}), 400

    user_email = request.form.get("user")
    question = request.form.get("question")

    AUDIO_URL = None

    try:
        audio_file = request.files["audio"]
        audio_buffer = audio_file.read()

        if not IS_PRODUCTION:
            with open("./audio.wav", "wb") as f:
                f.write(audio_buffer)

        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
            punctuate=True,
            filler_words=True,
            utterances=True,
            utt_split=10000,

        )

        if IS_PRODUCTION:
            buffer_data = vercel_blob.put(
                audio_file.filename,
                audio_buffer,
                {
                    "addRandomSuffix": "true",
                },
            )

            AUDIO_URL = {"url": buffer_data.get("url")}

            response = deepgram.listen.prerecorded.v("1").transcribe_url(
                AUDIO_URL, options
            )

            print(response.to_json(indent=4))

        else:
            payload: FileSource = {
                "buffer": audio_buffer,
            }
            response = deepgram.listen.prerecorded.v("1").transcribe_file(
                payload, options
            )

        userObject = User.query.filter_by(email=user_email).first()

        if userObject is None:
            return jsonify({"error": "User not found"}), 404

        userId = userObject.id

        analysis_results = analyze_audio(response)

        long_pauses, pause_durations, transcript, filler_word_count_json = (
            extract_analysis_results(analysis_results)
        )

        logging.info(f"Transcript: {transcript}")

        new_result = Result(
            user_id=userId,
            question_id=1,  # You may need to update this with the actual question ID
            question=question,
            transcript=transcript,
            audio_url=AUDIO_URL["url"] if AUDIO_URL else None,
            filler_words=filler_word_count_json,
            long_pauses=long_pauses,
            interview_date=datetime.utcnow(),
            pause_durations=(
                0 if len(pause_durations) == 0 else json.dumps(pause_durations)
            ),
        )
        db.session.add(new_result)
        db.session.commit()

        return jsonify(analysis_results)

    except json.JSONDecodeError:
        db.session.rollback()
        return jsonify({"error": "Invalid JSON in question field"}), 400
    except Exception as e:
        db.session.rollback()
        stack_trace = traceback.format_exc()
        logging.error(f"Exception occurred: {e}\n{stack_trace}")
        return jsonify({"error": "An internal error occurred. Please try again later."}), 500


@app.route("/service/save_video_url", methods=["POST"])
def save_video_url():
    try:
        data = request.get_json()
        user_email = data.get("user")
        video_url = data.get("video_url")
        user = user_email

        if not user_email or not video_url:
            return jsonify({"error": "User and video URL are required"}), 400

        logging.info(f"Received video URL: {video_url}")

        user_record = User.query.filter_by(email=user).first()
        if not user_record:
            return jsonify({"error": "User not found"}), 404

        userId = user_record.id
        last_updated_result = Result.query.filter_by(
            user_id=userId).order_by(desc(Result.updated_at)).first()
        if last_updated_result:
            last_updated_result.video_url = video_url
            db.session.commit()
            return jsonify({"message": "Video successfully uploaded to the database"}), 201
        else:
            return jsonify({"message": "No video has been added to the database"}), 404

    except Exception as e:
        logging.error(f"Error saving video URL: {e}")
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/service/generate_ai_response', methods=['POST'])
def generate_ai_response():
    try:
        data = request.get_json()
        name = data.get("name")
        company = data.get("company")
        position = data.get("position")
        interview_type = data.get("interview_type")
        question = data.get("question")
        user_email = data.get("user")

        # Log the received data
        logging.info(f"Received data: {data}")

        userObject = User.query.filter_by(email=user_email).first()
        if not userObject:
            logging.error('User not found')
            return jsonify({'error': 'User not found'}), 404

        userId = userObject.id
        result = Result.query.filter_by(
            user_id=userId).order_by(Result.id.desc()).first()

        if not result:
            logging.error('No results found for user')
            return jsonify({'error': 'No results found for the user'}), 404

        if not result.audio_url and not result.video_url:
            logging.error('No audio or video URL found in the result')
            return jsonify({'error': 'No audio or video URL found in the result'}), 400

        file_url = result.audio_url if result.audio_url else result.video_url
        logging.info(f"File URL retrieved from Result table: {file_url}")

        file_res = requests.get(file_url)
        if file_res.status_code != 200:
            logging.error('Failed to download file from the provided URL')
            return jsonify({'error': 'Failed to download file from the provided URL'}), 400

        file_path = '/tmp/audio_video_file'
        with open(file_path, 'wb') as f:
            f.write(file_res.content)

        if result.video_url:
            audio_path = '/tmp/extracted_audio.wav'
            # video_clip = VideoFileClip(file_path) # currently commented out because we are not using moviepy.
            # video_clip.audio.write_audiofile(audio_path)
            file_path = audio_path

        with open(file_path, 'rb') as audio_file:
            audio_content = audio_file.read()

        # Generate the AI feedback prompt using the user's information
        prompt = generate_feedback_prompt(
            name, company, position, interview_type, question)
        gemini_response = prompt_with_audio_file(audio_content, prompt)

        logging.info(f"Received response from Gemini: {gemini_response}")

        if isinstance(gemini_response, str):
            logging.info(f"Response text: {gemini_response}")
            extracted_data = extract_score_from_gemini_response(
                gemini_response)
            score = extracted_data["score"]
            if score is not None:
                logging.info(f"Extracted score: {score}")
                result.score = score
            else:
                logging.warning("No score extracted from the response")

            db.session.commit()
            return jsonify({"response": gemini_response})
        else:
            logging.error("Response text is not a string")
            if 'error' in gemini_response and '429' in gemini_response['error']:
                return jsonify({'response': 'Something went wrong. Our AI cannot analyze your answer right now. Please try again later.'}), 429

            return jsonify({"error": "Invalid response format from Gemini"}), 500

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        db.session.rollback()
        if "429" in str(e):
            return jsonify({"error": "Something went wrong. Our AI cannot analyze your answer right now. Please try again later."}), 429
        return jsonify({"error": str(e)}), 500
######


@app.route("/service/health", methods=["GET"])
def health():
    return {"status": "ok", "message": "API listening"}


@app.route("/service/add_user", methods=["POST"])
def add_email_route():
    data = request.json
    email = data["email"]

    if not email:
        return jsonify({"error": "Email is required"}), 400

    user = User.query.filter_by(email=email).first()

    if user:
        return jsonify({"error": "User already exists"}), 400

    new_user = User(email=email)
    db.session.add(new_user)
    db.session.commit()

    return jsonify({"message": "User added successfully", "user_id": new_user.id}), 201


@app.route("/service/get_users", methods=["GET"])
def get_emails_route():
    emails = User.query.all()
    return jsonify(emails)


@app.route("/service/add_question", methods=["POST"])
def add_question_route():
    try:
        data = request.get_json()
        question_text = data.get('question')

        if not question_text:
            return jsonify({"error": "Question field is required"}), 400

        new_question = Question(question=question_text)
        db.session.add(new_question)
        db.session.commit()

        return jsonify({"message": "Question added successfully"}, 201)

    except json.JSONDecodeError:
        db.session.rollback()
        return jsonify({"error": "Invalid JSON in request body"}), 400
    except Exception as e:
        stack_trace = traceback.format_exc()
        db.session.rollback()
        logging.error(f"Exception occurred: {e}\n{stack_trace}")
        return jsonify({"error": "An internal error occurred. Please try again later."}), 500


@app.route('/service/generate_interview_question', methods=['GET'])
def generate_interview_question():
    try:
        # Assuming you get the name, company, position, and interview_type from the request or session
        name = request.args.get("name")
        company = request.args.get("company")
        position = request.args.get("position")
        interview_type = request.args.get("interview_type")

        print(
            f"Name: {name}, Company: {company}, Position: {position}, Interview Type: {interview_type}")

        prompt = generate_interview_prompt(
            name, company, position, interview_type)

        gemini_response = text_prompt_for_question(prompt)

        if gemini_response:
            question = gemini_response
            new_question = Question(question=gemini_response)
            db.session.add(new_question)
            db.session.commit()
            return question
        else:
            return jsonify({"error": "No response from Gemini"}), 500
    except Exception as e:
        db.session.rollback()
        logging.error(f"Exception: {e}")
        if "429" in str(e):  # fall back in case quota for gemini is reached.
            logging.info("429 falling back to random question from database.")
            random_question = Question.query.order_by(
                db.func.random()).first()
            if random_question:
                return random_question.question

            else:
                return jsonify({"error": "No questions available in the database"}), 500

        return jsonify({"error": str(e)}), 500


@app.route("/service/save_results", methods=["POST"])
def save_results():
    try:
        data = request.get_json()

        logging.info(f"Received request data: {data}")

        user = data.get("user")
        results = data.get("results")

        if not user or not results:
            logging.error(f"Missing user or results: {data}")
            return jsonify({"error": "User and results are required"}), 400

        logging.info(f"Looking for user with email: {user}")
        userObj = User.query.filter_by(email=user).first()

        if not userObj:
            logging.error(f"User not found: {user}")
            return jsonify({"error": "User not found"}), 404

        userId = userObj.id
        logging.info(f"User found. ID: {userId}")

        for result in results:
            logging.info(
                f"Processing result for question: {result.get('question')}")

            existing_result = Result.query.filter_by(user_id=userId)\
                                          .order_by(Result.updated_at.desc())\
                                          .first()

            if existing_result:

                logging.info(
                    f"Updating most recent result for user ID: {userId}")
                existing_result.transcript = result.get("transcript")
                existing_result.filler_words = result.get("filler_words")
                existing_result.audio_url = result.get("audio_url")
                existing_result.long_pauses = result.get("long_pauses")
                existing_result.pause_durations = result.get("pause_durations")
                existing_result.ai_feedback = result.get("ai_feedback")
                existing_result.video_url = result.get("video_url")
                existing_result.updated_at = datetime.utcnow()
                logging.info({existing_result.get_as_dict()})
            else:

                logging.info(f"Inserting new result for user ID: {userId}")
                new_result = Result(
                    user_id=userId,
                    question_id=result.get("question_id"),
                    question=result.get("question"),
                    transcript=result.get("transcript"),
                    audio_url=result.get("audio_url"),
                    filler_words=result.get("filler_words"),
                    long_pauses=result.get("long_pauses"),
                    pause_durations=result.get("pause_durations"),
                    score=result.get("score"),
                    interview_date=datetime.utcnow(),
                    ai_feedback=result.get("ai_feedback"),
                    video_url=result.get("video_url"),
                    updated_at=datetime.utcnow()
                )
                db.session.add(new_result)

        db.session.commit()
        logging.info("Results saved successfully")
        return jsonify({"message": "Results saved successfully"}), 201

    except Exception as e:
        logging.error(f"Error occurred: {str(e)}")
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/service/get_results", methods=["GET"])
def get_results():
    try:
        user = request.args.get("user").strip()
        if not user:
            return jsonify({"error": "User is required"}), 400

        user_record = User.query.filter_by(email=user).first()
        if not user_record:
            return jsonify({"error": "User not found"}), 404

        userId = user_record.id
        last_updated_result = Result.query.filter_by(
            user_id=userId).order_by(desc(Result.updated_at)).first()
        if last_updated_result:
            return jsonify(last_updated_result.get_as_dict())
        else:
            return jsonify({"message": "No results found"}), 404

    except Exception as e:

        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@app.route('/service/get_all_results', methods=['GET'])
def get_all_results_for_user():
    try:
        user = request.args.get('user').strip()
        userId = User.query.filter_by(email=user).first().id

        results = Result.query.filter_by(user_id=userId).all()
        results_dict = [result.get_as_dict() for result in results]
        return jsonify(results_dict)
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return jsonify({'error': str(e)}), 500


@app.route("/service/delete_result/<int:result_id>", methods=["DELETE"])
def delete_result(result_id):
    try:
        # Find the result by its ID
        result = Result.query.get(result_id)

        if not result:
            return jsonify({"error": "Result not found"}), 404

        # Delete the result from the database
        db.session.delete(result)
        db.session.commit()

        return jsonify({"message": "Result deleted successfully"}), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(port=3001, debug=True)
