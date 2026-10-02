import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from docx import Document
from unittest.mock import patch

import app as qc_app


class AnalysisPauseTest(unittest.TestCase):
    def test_app_serves_checkmark_camera_brand_and_favicon(self):
        client = qc_app.app.test_client()

        page = client.get("/")
        icon = client.get("/static/video-reviewer-icon.svg")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'rel="icon"', page.data)
        self.assertIn(b'video-reviewer-icon.svg', page.data)
        self.assertIn(b'<img src="/static/video-reviewer-icon.svg"', page.data)
        self.assertEqual(icon.status_code, 200)
        self.assertIn(b'<svg', icon.data)
        self.assertIn(b'stroke="#9acfb0"', icon.data)
        page.close()
        icon.close()

    def test_script_pre_review_is_passed_to_later_ai_guidance(self):
        pid = f"pre-review-{uuid.uuid4().hex}"
        result = {
            "summary": "Use the supplied source for the population figure.",
            "checks": ["Verify the 2024 population value against the reference."],
            "conflicts": [{
                "script_quote": "Population is 40,000.",
                "reference_quote": "Population is 30,000.",
                "explanation": "The figures differ.",
            }],
        }
        model = unittest.mock.MagicMock()
        model.create_chat_completion.return_value = {
            "choices": [{"message": {"content": json.dumps(result)}}],
        }
        with tempfile.TemporaryDirectory() as project_dir:
            with open(os.path.join(project_dir, "script.txt"), "w", encoding="utf-8") as file:
                file.write("Population is 40,000.")
            with open(os.path.join(project_dir, "reference.txt"), "w", encoding="utf-8") as file:
                file.write("Population is 30,000.")
            qc_app.write_json(os.path.join(project_dir, "ai_instructions.json"), {
                "instructions": "Prioritize accurate population statistics.",
            })
            with patch.object(qc_app, "get_local_chat_model", return_value=model):
                pre_review = qc_app.run_script_pre_review(project_dir)

            guidance = qc_app.project_ai_guidance(project_dir)

        self.assertEqual(pre_review["state"], "done")
        self.assertIn("Population is 40,000", guidance)
        self.assertIn("Population is 30,000", guidance)
        self.assertIn("Prioritize accurate population statistics", guidance)

    def test_identical_script_and_reference_are_not_compared_as_separate_documents(self):
        script_text = "The population is 40,000.\nThe update was released in 2024."
        with tempfile.TemporaryDirectory() as project_dir:
            for filename in ("script.txt", "reference.txt"):
                with open(os.path.join(project_dir, filename), "w", encoding="utf-8") as file:
                    file.write(script_text)

            result = qc_app.run_script_pre_review(project_dir)

        self.assertEqual(result["state"], "skipped")
        self.assertIn("Add client instructions", result["message"])

    def test_automatic_visual_pre_review_promotes_concerns_into_issues(self):
        pid = f"visual-pre-review-{uuid.uuid4().hex}"
        analysis = {
            "info": {"duration": 60},
            "thumb_count": 60,
            "settings": {"max_shot_seconds": 6},
            "shots": [
                {"start": 10.0, "end": 12.0, "duration": 2.0, "thumb": 10},
                {"start": 20.0, "end": 22.0, "duration": 2.0, "thumb": 20},
            ],
            "issues": [],
        }
        findings = [
            {
                "assessment": "possible concern", "category": "Wrong footage",
                "observation": "A pot is shown instead of the vase named in narration.",
                "expected_subject": "vase", "observed_subject": "pot", "confidence": "high",
            },
            {
                "assessment": "possible concern", "category": "Wrong footage",
                "observation": "The animal may not match the narration.",
                "expected_subject": "mongoose", "observed_subject": "ferret", "confidence": "low",
            },
        ]
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "meta.json"), {
                "video_file": "video.mp4",
            })
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "installed_vision_model", return_value="qwen2.5vl:latest"),
                patch.object(qc_app, "video_file", return_value="video.mp4"),
                patch.object(qc_app, "sample_review_images", return_value=["encoded-frame"]),
                patch.object(qc_app, "review_frame", side_effect=findings),
            ):
                result = qc_app.run_automatic_visual_pre_review(pid, analysis)
                issues = qc_app.all_issues(project_path, analysis, {})

        self.assertEqual(result["state"], "done")
        self.assertEqual(len(issues), 2)
        self.assertEqual(issues[0]["category"], "Wrong footage")
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertIn("Expected vase; observed pot", issues[0]["title"])
        self.assertEqual(issues[1]["severity"], "check")
        self.assertEqual(issues[0]["confidence"], "high")
        self.assertEqual(issues[1]["source"], "visual_ai")

    def test_automatic_visual_pre_review_reports_missing_vision_model(self):
        pid = f"visual-pre-review-skip-{uuid.uuid4().hex}"
        analysis = {
            "shots": [{"start": 0.0, "end": 1.0, "duration": 1.0}],
        }
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "installed_vision_model", return_value=None),
            ):
                result = qc_app.run_automatic_visual_pre_review(pid, analysis)
                saved = qc_app.read_json(os.path.join(project_path, "ollama_review.json"), {})

        self.assertEqual(result["state"], "skipped")
        self.assertEqual(saved["state"], "skipped")
        self.assertIn("Qwen2.5-VL", saved["message"])

    def test_project_reference_docx_upload_saves_extracted_text(self):
        pid = f"reference-upload-{uuid.uuid4().hex}"
        document_buffer = io.BytesIO()
        document = Document()
        document.add_paragraph("Authoritative project facts")
        document.save(document_buffer)
        with tempfile.TemporaryDirectory() as projects_dir:
            project_dir = os.path.join(projects_dir, pid)
            os.mkdir(project_dir)
            qc_app.write_json(os.path.join(project_dir, "meta.json"), {"name": "test.mp4"})
            with patch.object(qc_app, "PROJECTS_DIR", projects_dir):
                response = qc_app.app.test_client().post(
                    f"/api/projects/{pid}/reference",
                    data=document_buffer.getvalue(),
                    headers={"X-Filename": "facts.docx"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["reference_name"], "facts.docx")
            self.assertEqual(qc_app.project_reference_text(project_dir), "Authoritative project facts")

    def test_client_instructions_save_before_analysis(self):
        pid = f"instructions-test-{uuid.uuid4().hex}"
        instructions = "Prioritize British English spelling."
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "meta.json"), {"name": "test.mp4"})
            with patch.object(qc_app, "PROJECTS_DIR", projects_dir):
                response = qc_app.app.test_client().put(
                    f"/api/projects/{pid}/ai-instructions",
                    json={"instructions": instructions},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(qc_app.project_ai_instructions(project_path), instructions)
            self.assertEqual(
                qc_app.read_json(os.path.join(project_path, "meta.json"))["ai_instructions"],
                instructions,
            )

    def test_assistant_image_validation_accepts_jpeg_data_url(self):
        image = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffdata").decode("ascii")
        self.assertEqual(qc_app.parse_assistant_images([image]), [image.split(",", 1)[1]])

    def test_assistant_image_validation_rejects_non_jpeg(self):
        with self.assertRaisesRegex(ValueError, "JPEG"):
            qc_app.parse_assistant_images(["data:image/png;base64,aW1hZ2U="])

    def test_assistant_image_validation_limits_attachment_count(self):
        with self.assertRaisesRegex(ValueError, "no more than 4"):
            qc_app.parse_assistant_images(["x"] * 5)

    def test_assistant_image_message_uses_local_vision_model(self):
        pid = f"assistant-image-{uuid.uuid4().hex}"
        image_data = base64.b64encode(b"\xff\xd8\xffimage").decode("ascii")
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "analysis.json"), {
                "info": {"duration": 60, "fps": 30},
                "transcript": [],
                "text_events": [],
                "issues": [],
            })
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "installed_vision_model", return_value="llava:latest"),
                patch.object(qc_app, "ollama_request", return_value={"message": {"content": "A test image."}}) as request,
            ):
                response = qc_app.app.test_client().post(
                    f"/api/projects/{pid}/assistant",
                    json={"message": "What is shown?", "images": [f"data:image/jpeg;base64,{image_data}"]},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["answer"], "A test image.")
        self.assertEqual(response.get_json()["model"], "llava:latest")
        request_payload = request.call_args.args[1]
        self.assertEqual(request_payload["model"], "llava:latest")
        self.assertEqual(request_payload["messages"][-1]["images"], [image_data])

    def test_assistant_dismisses_all_matching_findings(self):
        pid = f"assistant-dismiss-{uuid.uuid4().hex}"
        issues = [
            {
                "id": "low-quality-1", "category": "Low quality image",
                "title": "Blurry or low-quality picture", "detail": "", "time": 10.0,
            },
            {
                "id": "low-quality-2", "category": "Low quality image",
                "title": "Blurry or low-quality picture", "detail": "", "time": 20.0,
            },
            {
                "id": "audio-1", "category": "Audio issue",
                "title": "Audio peaks", "detail": "", "time": 30.0,
            },
        ]
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "analysis.json"), {
                "info": {"duration": 60, "fps": 30},
                "transcript": [],
                "text_events": [],
                "issues": issues,
            })
            with patch.object(qc_app, "PROJECTS_DIR", projects_dir):
                response = qc_app.app.test_client().post(
                    f"/api/projects/{pid}/assistant",
                    json={"message": "Can you delete the blurry and low quality in the issues tab, all of it?"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["issues_updated"])
            self.assertIn("Dismissed 2", response.get_json()["answer"])
            saved_review = qc_app.read_json(os.path.join(project_path, "review.json"), {})

        self.assertEqual(saved_review["status"]["low-quality-1"]["status"], "dismissed")
        self.assertEqual(saved_review["status"]["low-quality-2"]["status"], "dismissed")
        self.assertNotIn("audio-1", saved_review.get("status", {}))

    def test_assistant_starts_new_topics_without_old_chat_history(self):
        pid = f"assistant-new-topic-{uuid.uuid4().hex}"
        model = unittest.mock.MagicMock()
        model.create_chat_completion.return_value = {
            "choices": [{"message": {"content": "Issue categories group similar findings."}}],
        }
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "analysis.json"), {
                "info": {"duration": 60, "fps": 30},
                "transcript": [],
                "text_events": [],
                "issues": [],
            })
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "get_local_chat_model", return_value=model),
            ):
                response = qc_app.app.test_client().post(
                    f"/api/projects/{pid}/assistant",
                    json={
                        "message": "Can you explain how issue categories work?",
                        "history": [
                            {"role": "user", "content": "Please review these images."},
                            {"role": "assistant", "content": "Please provide the images for review."},
                        ],
                    },
                )

        self.assertEqual(response.status_code, 200)
        sent_messages = model.create_chat_completion.call_args.kwargs["messages"]
        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[-1]["content"], "Can you explain how issue categories work?")

    def test_assistant_keeps_bounded_server_history_for_followups(self):
        pid = f"assistant-history-{uuid.uuid4().hex}"
        model = unittest.mock.MagicMock()
        model.create_chat_completion.side_effect = [
            {"choices": [{"message": {"content": "The population issue needs checking."}}]},
            {"choices": [{"message": {"content": "Compare the spoken number with the overlay."}}]},
        ]
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "analysis.json"), {
                "info": {"duration": 60, "fps": 30}, "transcript": [], "text_events": [], "issues": [],
            })
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "get_local_chat_model", return_value=model),
            ):
                client = qc_app.app.test_client()
                first = client.post(
                    f"/api/projects/{pid}/assistant",
                    json={"message": "Explain the population discrepancy."},
                )
                follow_up = client.post(
                    f"/api/projects/{pid}/assistant",
                    json={
                        "message": "And what about that?",
                        "history": [{"role": "user", "content": "Client supplied unrelated history."}],
                    },
                )
                saved = qc_app.read_json(os.path.join(project_path, "assistant_history.json"), [])

        self.assertEqual(first.status_code, 200)
        self.assertEqual(follow_up.status_code, 200)
        followup_prompt = model.create_chat_completion.call_args.kwargs["messages"]
        self.assertTrue(any("Explain the population discrepancy." in item["content"] for item in followup_prompt))
        self.assertFalse(any("Client supplied unrelated history." in item["content"] for item in followup_prompt))
        self.assertEqual(len(saved), 4)

    def test_assistant_can_be_given_explicit_issue_context(self):
        pid = f"assistant-issue-context-{uuid.uuid4().hex}"
        issue = {
            "id": "population-1", "category": "Years", "time": 12.0,
            "title": "Population figure differs", "detail": "Speech says 30,000; text shows 40,000.",
        }
        model = unittest.mock.MagicMock()
        model.create_chat_completion.return_value = {
            "choices": [{"message": {"content": "Compare the source figures at this timecode."}}],
        }
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "analysis.json"), {
                "info": {"duration": 60, "fps": 30}, "transcript": [], "text_events": [], "issues": [issue],
            })
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "get_local_chat_model", return_value=model),
            ):
                response = qc_app.app.test_client().post(
                    f"/api/projects/{pid}/assistant",
                    json={"message": "What should I check here?", "issue_id": issue["id"]},
                )

        self.assertEqual(response.status_code, 200)
        sent_system_prompt = model.create_chat_completion.call_args.kwargs["messages"][0]["content"]
        self.assertIn(issue["title"], sent_system_prompt)
        self.assertIn('"time": 12.0', sent_system_prompt)

    def test_assistant_history_clear_endpoint_deletes_saved_conversation(self):
        pid = f"assistant-history-clear-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            history_path = os.path.join(project_path, "assistant_history.json")
            qc_app.write_json(history_path, [{"role": "assistant", "content": "Old response."}])
            with patch.object(qc_app, "PROJECTS_DIR", projects_dir):
                response = qc_app.app.test_client().delete(f"/api/projects/{pid}/assistant-history")
                saved_history = qc_app.read_json(history_path)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved_history, [])

    def test_assistant_recognizes_short_follow_ups_without_mistaking_full_requests(self):
        self.assertTrue(qc_app.assistant_is_follow_up("And what about that?"))
        self.assertTrue(qc_app.assistant_is_follow_up("Dismiss them"))
        self.assertFalse(qc_app.assistant_is_follow_up("It still requires an image"))
        self.assertFalse(qc_app.assistant_is_follow_up("Can you explain how issue categories work?"))

    def test_local_ai_cross_check_surfaces_model_errors(self):
        model = unittest.mock.MagicMock()
        model.create_chat_completion.side_effect = RuntimeError("mock inference failure")
        analysis = {
            "text_events": [{
                "score": 0.9,
                "text": "Population 40,000",
                "start": 10.0,
                "end": 12.0,
            }],
            "transcript": [{
                "start": 10.0,
                "end": 12.0,
                "text": "The population reached 30,000.",
            }],
        }
        with patch.object(qc_app, "get_local_chat_model", return_value=model):
            with self.assertRaisesRegex(RuntimeError, "batch 1 of 1.*mock inference failure"):
                qc_app.local_ai_text_flags(analysis)

    def test_local_ai_cross_check_preserves_completed_batch_results(self):
        first_result = {
            "choices": [{"message": {"content": json.dumps({"findings": [{
                "event_id": 0, "source_pair": "narration_screen",
                "spoken_quote": "30 percent", "screen_quote": "40%",
                "reason": "The percentage values differ.", "confidence": "high",
            }]})}}],
        }
        model = unittest.mock.MagicMock()
        model.create_chat_completion.side_effect = [first_result, RuntimeError("second batch failed")]
        events = [{
            "score": 0.95, "text": "Approval rate 40%", "start": index * 10.0, "end": index * 10.0 + 1,
        } for index in range(9)]
        transcript = [{
            "start": index * 10.0, "end": index * 10.0 + 1, "text": "The approval rate reached 30 percent.",
        } for index in range(9)]
        completed_batches = []
        with patch.object(qc_app, "get_local_chat_model", return_value=model):
            with self.assertRaisesRegex(RuntimeError, "batch 2 of 2"):
                qc_app.local_ai_text_flags(
                    {"text_events": events, "transcript": transcript},
                    on_batch=lambda current, total, issues: completed_batches.append((current, total, issues)),
                )

        self.assertEqual(len(completed_batches), 1)
        self.assertEqual(completed_batches[0][:2], (1, 2))
        self.assertEqual(len(completed_batches[0][2]), 1)

    def test_ai_cleanup_preserves_completed_batch_suggestions_when_later_batch_fails(self):
        first_result = {
            "choices": [{"message": {"content": json.dumps({"reviews": [{
                "id": "year-0", "decision": "dismiss", "confidence": "high",
                "evidence_quote": "The narration says 2024.",
                "reason": "The transcript confirms the displayed year.",
            }]})}}],
        }
        model = unittest.mock.MagicMock()
        model.create_chat_completion.side_effect = [first_result, RuntimeError("second audit batch failed")]
        issues = [{
            "id": f"year-{index}", "category": "Years", "time": float(index * 30),
            "title": "Year on screen", "detail": "Check the displayed year.",
            "status": "open",
        } for index in range(5)]
        analysis = {
            "transcript": [{
                "start": float(index * 30), "text": "The narration says 2024.",
            } for index in range(5)],
            "text_events": [],
        }
        saved_batches = []
        with patch.object(qc_app, "get_local_chat_model", return_value=model):
            with self.assertRaisesRegex(RuntimeError, "batch 2 of 2"):
                qc_app.local_ai_cleanup_suggestions(
                    analysis, issues,
                    on_batch=lambda current, total, suggestions: saved_batches.append(
                        (current, total, suggestions)
                    ),
                )

        self.assertEqual(len(saved_batches), 1)
        self.assertEqual(saved_batches[0][:2], (1, 2))
        self.assertEqual([item["issue_id"] for item in saved_batches[0][2]], ["year-0"])

    def test_local_ai_worker_saves_partial_issues_and_batch_progress_on_failure(self):
        pid = f"partial-ai-worker-{uuid.uuid4().hex}"
        first_result = {
            "choices": [{"message": {"content": json.dumps({"findings": [{
                "event_id": 0, "source_pair": "narration_screen",
                "spoken_quote": "30 percent", "screen_quote": "40%",
                "reason": "The percentage values differ.", "confidence": "high",
            }]})}}],
        }
        model = unittest.mock.MagicMock()
        model.create_chat_completion.side_effect = [first_result, RuntimeError("second batch failed")]

        class OneJobQueue:
            def __init__(self):
                self.delivered = False

            def get(self):
                if self.delivered:
                    raise StopIteration
                self.delivered = True
                return pid

            def task_done(self):
                pass

        with tempfile.TemporaryDirectory() as projects_dir:
            project_dir = os.path.join(projects_dir, pid)
            os.mkdir(project_dir)
            events = [{
                "score": 0.95, "text": "Approval rate 40%",
                "start": index * 10.0, "end": index * 10.0 + 1,
            } for index in range(9)]
            transcript = [{
                "start": index * 10.0, "end": index * 10.0 + 1,
                "text": "The approval rate reached 30 percent.",
            } for index in range(9)]
            qc_app.write_json(os.path.join(project_dir, "analysis.json"), {
                "text_events": events, "transcript": transcript,
            })
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "local_ai_jobs", OneJobQueue()),
                patch.object(qc_app, "lower_worker_priority"),
                patch.object(qc_app, "get_local_chat_model", return_value=model),
            ):
                with self.assertRaises(StopIteration):
                    qc_app.local_ai_worker()
                saved_issues = qc_app.read_json(os.path.join(project_dir, "local_ai_issues.json"), [])
                saved_status = qc_app.read_json(os.path.join(project_dir, "local_ai_status.json"), {})

        self.assertEqual(len(saved_issues), 1)
        self.assertEqual(saved_status["state"], "error")
        self.assertEqual(saved_status["count"], 1)
        self.assertEqual(saved_status["completed_batches"], 1)
        self.assertEqual(saved_status["total_batches"], 2)

    def test_runtime_configuration_can_override_data_and_model_cache_paths(self):
        with patch.dict(os.environ, {
            "VIDEO_QC_DATA_DIR": "~/portable-video-qc/projects",
            "VIDEO_QC_MODEL_CACHE_DIR": "~/portable-video-qc/models",
        }):
            data_path = qc_app.configured_data_dir()
            model_path = qc_app.configured_model_cache_dir()

        self.assertEqual(data_path, os.path.abspath(os.path.expanduser("~/portable-video-qc/projects")))
        self.assertEqual(model_path, os.path.abspath(os.path.expanduser("~/portable-video-qc/models")))

    def test_ollama_url_rejects_credentials_and_non_service_paths(self):
        for value in ("http://user:password@localhost:11434", "http://localhost:11434/api"):
            with patch.dict(os.environ, {"VIDEO_QC_OLLAMA_URL": value}):
                with self.assertRaisesRegex(ValueError, "service base URL"):
                    qc_app.configured_ollama_url()

    def test_ai_readiness_reports_text_and_vision_model_status(self):
        with (
            patch.object(qc_app, "local_chat_model", object()),
            patch.object(qc_app, "ollama_request", return_value={
                "models": [{"name": "llava:latest"}, {"name": "llama3:latest"}],
            }),
        ):
            response = qc_app.app.test_client().get("/api/ai/status")

        self.assertEqual(response.status_code, 200)
        status = response.get_json()
        self.assertEqual(status["text"]["state"], "loaded")
        self.assertEqual(status["vision"]["state"], "ready")
        self.assertEqual(status["vision"]["model"], "llava:latest")

    def test_storage_cleanup_does_not_offer_project_data_as_model_cache(self):
        with tempfile.TemporaryDirectory() as data_dir:
            project_dir = os.path.join(data_dir, "sample-project")
            os.mkdir(project_dir)
            qc_app.write_json(os.path.join(project_dir, "meta.json"), {"name": "sample.mp4"})
            with (
                patch.object(qc_app, "PROJECTS_DIR", data_dir),
                patch.object(qc_app, "MODEL_CACHE_DIR", data_dir),
            ):
                candidates = qc_app.storage_candidates()

        self.assertNotIn("video-qc-model-cache", {item["id"] for item in candidates})

    def test_load_audio_normalizes_samples_without_changing_values(self):
        raw_audio = b"\x00\x40" * 1600
        process = unittest.mock.MagicMock()
        process.stdout.read.side_effect = [raw_audio, b""]
        process.returncode = 0
        with patch.object(qc_app.analyzer.subprocess, "Popen", return_value=process):
            samples = qc_app.analyzer.load_audio("video.mp4", total_duration=0.1)

        self.assertEqual(samples.dtype, qc_app.analyzer.np.float32)
        self.assertEqual(samples.shape, (1600,))
        self.assertTrue((samples == 0.5).all())

    def test_pause_rejects_completed_analysis(self):
        pid = f"done-test-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "status.json"), {"state": "done"})
            previous_projects_dir = qc_app.PROJECTS_DIR
            qc_app.PROJECTS_DIR = projects_dir
            try:
                response = qc_app.app.test_client().post(f"/api/projects/{pid}/pause")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(qc_app.read_json(os.path.join(project_path, "status.json"), {})["state"], "done")
            finally:
                qc_app.PROJECTS_DIR = previous_projects_dir

    def test_corrupt_json_returns_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "broken.json")
            with open(path, "w", encoding="utf-8") as json_file:
                json_file.write("{broken")
            self.assertEqual(qc_app.read_json(path, {"fallback": True}), {"fallback": True})

    def test_visual_review_sampling_is_bounded(self):
        shots = [{"start": index, "duration": 1} for index in range(40)]
        selected = qc_app.sample_shots(shots, qc_app.MAX_AUTOMATIC_VISUAL_SHOTS)
        self.assertEqual(len(selected), qc_app.MAX_AUTOMATIC_VISUAL_SHOTS)

    def test_visual_review_sampling_prioritizes_script_subjects(self):
        shots = [
            {"start": index * 2, "end": index * 2 + 1, "duration": 1, "narration": "Unrelated landscape footage"}
            for index in range(40)
        ]
        shots[23]["narration"] = "A capybara swims through the water"

        selected = qc_app.sample_shots(shots, 1, script_text="capybara conservation")

        self.assertEqual([index for index, _ in selected], [23])

    def test_automatic_visual_pre_review_uses_requested_sample_count_and_records_coverage(self):
        pid = f"visual-sample-count-{uuid.uuid4().hex}"
        analysis = {
            "shots": [
                {"start": float(index), "end": float(index + 1), "duration": 1, "thumb": index}
                for index in range(30)
            ],
            "thumb_count": 30,
        }
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "installed_vision_model", return_value="qwen2.5vl:latest"),
                patch.object(qc_app, "video_file", return_value="video.mp4"),
                patch.object(qc_app, "sample_review_images", return_value=["encoded-frame"]),
                patch.object(qc_app, "review_frame", return_value={
                    "assessment": "match", "category": "Wrong footage", "observation": "Matches narration.",
                    "expected_subject": "", "observed_subject": "", "confidence": "high",
                }),
            ):
                result = qc_app.run_automatic_visual_pre_review(pid, analysis, sample_count=8)

        self.assertEqual(result["state"], "done")
        self.assertEqual(result["total"], 8)
        self.assertEqual(result["total_shots"], 30)

    def test_project_creation_validates_and_persists_visual_sample_count(self):
        with tempfile.TemporaryDirectory() as projects_dir:
            with patch.object(qc_app, "PROJECTS_DIR", projects_dir):
                client = qc_app.app.test_client()
                invalid = client.post(
                    "/api/projects",
                    data=b"video",
                    headers={
                        "X-Filename": "sample.mp4", "X-Model": "base", "X-Language": "en",
                        "X-Visual-Sample-Count": "16",
                    },
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(os.listdir(projects_dir), [])

                response = client.post(
                    "/api/projects",
                    data=b"video",
                    headers={
                        "X-Filename": "sample.mp4", "X-Model": "base", "X-Language": "en",
                        "X-Visual-Sample-Count": "24",
                    },
                )
                self.assertEqual(response.status_code, 200)
                project_id = response.get_json()["id"]
                meta = qc_app.read_json(os.path.join(projects_dir, project_id, "meta.json"), {})

        self.assertEqual(meta["visual_sample_count"], 24)

    def test_worker_persists_visual_results_and_exposes_them_with_issues(self):
        pid = f"visual-worker-{uuid.uuid4().hex}"
        analysis = {
            "info": {"duration": 2, "fps": 30},
            "thumb_count": 2,
            "settings": {"max_shot_seconds": 6},
            "shots": [
                {"start": 0.0, "end": 1.0, "duration": 1.0, "thumb": 0},
                {"start": 1.0, "end": 2.0, "duration": 1.0, "thumb": 1},
            ],
            "issues": [],
            "warnings": [],
        }
        findings = [
            {
                "assessment": "possible concern", "category": "Wrong footage",
                "observation": "A pot appears instead of the vase in the script.",
                "expected_subject": "vase", "observed_subject": "pot", "confidence": "high",
            },
            {
                "assessment": "possible concern", "category": "Wrong footage",
                "observation": "The second sample may show a different animal.",
                "expected_subject": "mongoose", "observed_subject": "ferret", "confidence": "low",
            },
        ]

        class OneJobQueue:
            def __init__(self):
                self.delivered = False

            def get(self):
                if self.delivered:
                    raise StopIteration
                self.delivered = True
                return pid

            def task_done(self):
                pass

        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "meta.json"), {
                "name": "sample.mp4", "video_file": "video.mp4", "visual_sample_count": 8,
            })

            def analyze_video(*args, **kwargs):
                qc_app.write_json(os.path.join(project_path, "analysis.json"), analysis)
                return analysis

            with (
                patch.object(qc_app, "PROJECTS_DIR", projects_dir),
                patch.object(qc_app, "jobs", OneJobQueue()),
                patch.object(qc_app, "lower_worker_priority"),
                patch.object(qc_app, "pause_at_checkpoint"),
                patch.object(qc_app, "run_script_pre_review", return_value={"state": "skipped"}),
                patch.object(qc_app.analyzer, "analyze", side_effect=analyze_video),
                patch.object(qc_app, "local_ai_text_flags", return_value=[]),
                patch.object(qc_app, "installed_vision_model", return_value="qwen2.5vl:latest"),
                patch.object(qc_app, "video_file", return_value="video.mp4"),
                patch.object(qc_app, "sample_review_images", return_value=["encoded-frame"]),
                patch.object(qc_app, "review_frame", side_effect=findings),
            ):
                with self.assertRaises(StopIteration):
                    qc_app.worker()

                response = qc_app.app.test_client().get(f"/api/projects/{pid}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["visual_review"]["state"], "done")
        self.assertEqual(payload["visual_review"]["total_shots"], 2)
        self.assertEqual(len([item for item in payload["issues"] if item["source"] == "visual_ai"]), 2)

    def test_delete_rejects_active_analysis(self):
        pid = f"delete-test-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "status.json"), {"state": "running"})
            previous_projects_dir = qc_app.PROJECTS_DIR
            qc_app.PROJECTS_DIR = projects_dir
            try:
                response = qc_app.app.test_client().delete(f"/api/projects/{pid}")
                self.assertEqual(response.status_code, 409)
                self.assertTrue(os.path.isdir(project_path))
            finally:
                qc_app.PROJECTS_DIR = previous_projects_dir

    def test_cleanup_rejects_unknown_item_ids(self):
        response = qc_app.app.test_client().post(
            "/api/storage/cleanup",
            json={"items": ["C:\\not-a-storage-candidate"]},
        )
        self.assertEqual(response.status_code, 409)

    def test_permanent_delete_removes_disposable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "discard-me")
            os.mkdir(path)
            with open(os.path.join(path, "video.tmp"), "w", encoding="utf-8") as file:
                file.write("temporary")
            qc_app.permanent_delete_path(path)
            self.assertFalse(os.path.exists(path))

    def test_upload_rejects_non_video_extension(self):
        with tempfile.TemporaryDirectory() as projects_dir:
            previous_projects_dir = qc_app.PROJECTS_DIR
            qc_app.PROJECTS_DIR = projects_dir
            try:
                response = qc_app.app.test_client().post(
                    "/api/projects",
                    data=b"not executable",
                    headers={"X-Filename": "payload.exe", "X-Model": "base", "X-Language": "en"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(os.listdir(projects_dir), [])
            finally:
                qc_app.PROJECTS_DIR = previous_projects_dir

    def test_video_metadata_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            project_path = os.path.join(directory, "project")
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "meta.json"), {"video_file": "..\\outside.mp4"})
            with self.assertRaises(ValueError):
                qc_app.video_file(project_path)

    def test_name_checker_filters_places_and_organizations(self):
        self.assertFalse(qc_app.analyzer.probable_person_name("Erath County"))
        self.assertFalse(qc_app.analyzer.probable_person_name("Fort Worth"))
        self.assertFalse(qc_app.analyzer.probable_person_name("USDA National Wildlife"))
        self.assertTrue(qc_app.analyzer.probable_person_name("Tim Smeiser"))

    def test_repeated_spelling_flags_are_consolidated(self):
        issues = [
            {"id": "a1", "category": "Spelling", "title": 'Possible spelling error: "latendy"',
             "time": 10.0, "end": 11.0, "detail": 'In "viral latendy".'},
            {"id": "a2", "category": "Spelling", "title": 'Possible spelling error: "latendy"',
             "time": 70.0, "end": 71.0, "detail": 'In "viral latendy".'},
        ]
        result = qc_app.analyzer.consolidate_repeat_issues(issues)
        self.assertEqual(len(result), 1)
        self.assertEqual([item["time"] for item in result[0]["occurrences"]], [10.0, 70.0])

    def test_nearby_visibility_flags_group_but_distant_flags_do_not(self):
        issues = [
            {"id": "a1", "category": "Text not visible", "title": "On-screen text touches the frame edge",
             "time": 10.0, "end": 11.0, "detail": '"TITLE" may be cut off.'},
            {"id": "a2", "category": "Text not visible", "title": "On-screen text touches the frame edge",
             "time": 11.5, "end": 12.0, "detail": '"TITLE" may be cut off.'},
            {"id": "a3", "category": "Text not visible", "title": "On-screen text touches the frame edge",
             "time": 40.0, "end": 41.0, "detail": '"TITLE" may be cut off.'},
        ]
        result = qc_app.analyzer.consolidate_repeat_issues(issues)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]["occurrences"]), 2)
        self.assertNotIn("occurrences", result[1])

    def test_number_formatting_flags_unseparated_thousands_only(self):
        issues = qc_app.analyzer.Issues()
        events = [{
            "text": "Population 40000; updated 2024; decimal 40000.5; code AB40000X; already 40,000.",
            "start": 12.0,
            "end": 13.0,
        }]
        qc_app.analyzer.check_number_formatting(events, issues)
        self.assertEqual(len(issues.items), 1)
        self.assertEqual(issues.items[0]["category"], "Number formatting")
        self.assertIn("40,000", issues.items[0]["detail"])

    def test_number_formatting_distinguishes_year_from_quantity(self):
        issues = qc_app.analyzer.Issues()
        events = [{
            "text": "In 2026, the population reached 2026 animals; the report is dated 2024.",
            "start": 20.0,
            "end": 21.0,
        }]
        qc_app.analyzer.check_number_formatting(events, issues)
        self.assertEqual(len(issues.items), 1)
        self.assertIn("2,026", issues.items[0]["detail"])

    def test_pause_and_resume_at_worker_checkpoint(self):
        pid = f"pause-test-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as projects_dir:
            project_path = os.path.join(projects_dir, pid)
            os.mkdir(project_path)
            qc_app.write_json(os.path.join(project_path, "status.json"), {"state": "running"})

            previous_projects_dir = qc_app.PROJECTS_DIR
            with qc_app.analysis_pause_lock:
                qc_app.analysis_resume_events.pop(pid, None)
            qc_app.PROJECTS_DIR = projects_dir
            checkpoint_finished = threading.Event()
            worker_thread = None
            try:
                client = qc_app.app.test_client()
                pause_response = client.post(f"/api/projects/{pid}/pause")
                self.assertEqual(pause_response.get_json()["state"], "pausing")

                def reach_checkpoint():
                    qc_app.pause_at_checkpoint(pid, project_path, 12, "Scanning", "motion", 35)
                    checkpoint_finished.set()

                worker_thread = threading.Thread(target=reach_checkpoint)
                worker_thread.start()

                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    status = qc_app.read_json(os.path.join(project_path, "status.json"), {})
                    if status.get("state") == "paused":
                        break
                    time.sleep(0.01)
                self.assertEqual(status.get("state"), "paused")
                self.assertFalse(checkpoint_finished.is_set())

                resume_response = client.post(f"/api/projects/{pid}/resume")
                self.assertEqual(resume_response.get_json()["state"], "resuming")
                worker_thread.join(timeout=2)
                self.assertFalse(worker_thread.is_alive())
                self.assertTrue(checkpoint_finished.is_set())
                self.assertEqual(client.get(f"/api/projects/{pid}/status").get_json()["state"], "running")
            finally:
                with qc_app.analysis_pause_lock:
                    resume_event = qc_app.analysis_resume_events.pop(pid, None)
                if resume_event:
                    resume_event.set()
                if worker_thread and worker_thread.is_alive():
                    worker_thread.join(timeout=2)
                qc_app.PROJECTS_DIR = previous_projects_dir


if __name__ == "__main__":
    unittest.main()
