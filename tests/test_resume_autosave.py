import csv
import io
import json
import os
import tempfile
import unittest

import app as image_ranker_app


class ResumeAutosaveTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = self.temp_dir.name
        self.dataset_dir = os.path.join(self.base_dir, "dataset")
        os.makedirs(self.dataset_dir)

        self.image_paths = []
        for filename in ("a.jpg", "b.jpg", "c.jpg"):
            image_path = os.path.join(self.dataset_dir, filename)
            with open(image_path, "wb") as f:
                f.write(b"test")
            self.image_paths.append(image_path)

        self.autosave_file = os.path.join(
            self.dataset_dir,
            "comparisons_autosave_2026-03-18.csv",
        )
        with open(self.autosave_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Winner", "Loser"])
            writer.writerow([self.image_paths[0], self.image_paths[1]])

        exclusions_file = image_ranker_app.get_exclusions_file_path(self.autosave_file)
        with open(exclusions_file, "w") as f:
            json.dump({self.image_paths[2]: "duplicate"}, f)

        image_ranker_app.BASE_DIR = self.base_dir
        image_ranker_app.IMAGE_FOLDER = "static/images"
        image_ranker_app.current_directory = None
        image_ranker_app.elo_ranking = image_ranker_app.TrueSkillRanking()
        image_ranker_app.excluded_images = {}
        image_ranker_app.image_pairs = []
        image_ranker_app.current_pair_index = 0
        image_ranker_app.context_data = None
        image_ranker_app.comparisons_since_autosave = 0
        self.client = image_ranker_app.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_set_directory_restores_exclusions_before_building_pairs(self):
        response = self.client.post(
            "/set_directory",
            data={
                "path": "dataset",
                "autosaveFile": "dataset/comparisons_autosave_2026-03-18.csv",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(
            image_ranker_app.excluded_images,
            {self.image_paths[2]: "duplicate"},
        )

        remaining_images = {image for pair in image_ranker_app.image_pairs for image in pair}
        self.assertNotIn(self.image_paths[2], remaining_images)

    def test_set_directory_loads_autosave_rankings(self):
        response = self.client.post(
            "/set_directory",
            data={
                "path": "dataset",
                "autosaveFile": "dataset/comparisons_autosave_2026-03-18.csv",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(image_ranker_app.elo_ranking.comparison_history), 1)
        rankings = image_ranker_app.elo_ranking.get_rankings()
        self.assertEqual(len(rankings), 2)
        self.assertEqual(rankings[0][0], self.image_paths[0])
        self.assertEqual(rankings[1][0], self.image_paths[1])

    def test_manual_import_autosave_rebuilds_session_state(self):
        self.client.post(
            "/set_directory",
            data={"path": "dataset", "autosaveFile": ""},
        )

        image_ranker_app.excluded_images = {"stale.jpg": "old"}
        image_ranker_app.initialize_image_pairs()

        with open(self.autosave_file, "rb") as f:
            response = self.client.post(
                "/import_comparison_history",
                data={
                    "file": (io.BytesIO(f.read()), os.path.basename(self.autosave_file)),
                    "append": "false",
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(len(image_ranker_app.elo_ranking.comparison_history), 1)
        self.assertEqual(
            image_ranker_app.excluded_images,
            {self.image_paths[2]: "duplicate"},
        )
        remaining_images = {image for pair in image_ranker_app.image_pairs for image in pair}
        self.assertNotIn(self.image_paths[2], remaining_images)

    def test_manual_import_non_autosave_preserves_existing_exclusions(self):
        self.client.post(
            "/set_directory",
            data={"path": "dataset", "autosaveFile": ""},
        )

        image_ranker_app.excluded_images = {self.image_paths[2]: "duplicate"}
        image_ranker_app.initialize_image_pairs()

        exported_comparisons_text = io.StringIO()
        writer = csv.writer(exported_comparisons_text)
        writer.writerow(["Winner", "Loser"])
        writer.writerow([self.image_paths[0], self.image_paths[1]])
        writer.writerow([self.image_paths[1], self.image_paths[0]])
        exported_comparisons = io.BytesIO(exported_comparisons_text.getvalue().encode("utf-8"))

        response = self.client.post(
            "/import_comparison_history",
            data={
                "file": (exported_comparisons, "comparisons.csv"),
                "append": "false",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(
            image_ranker_app.excluded_images,
            {self.image_paths[2]: "duplicate"},
        )
        remaining_images = {image for pair in image_ranker_app.image_pairs for image in pair}
        self.assertNotIn(self.image_paths[2], remaining_images)


if __name__ == "__main__":
    unittest.main()
