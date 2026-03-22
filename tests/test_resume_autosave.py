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
        self.base_dir = os.path.realpath(self.temp_dir.name)
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

        skipped_pairs_file = image_ranker_app.get_skipped_pairs_file_path(self.autosave_file)
        with open(skipped_pairs_file, "w") as f:
            json.dump([[self.image_paths[0], self.image_paths[2]]], f)

        image_ranker_app.BASE_DIR = self.base_dir
        image_ranker_app.IMAGE_FOLDER = "static/images"
        image_ranker_app.current_directory = None
        image_ranker_app.elo_ranking = image_ranker_app.TrueSkillRanking()
        image_ranker_app.excluded_images = {}
        image_ranker_app.image_pairs = []
        image_ranker_app.skipped_pairs = set()
        image_ranker_app.current_pair_index = 0
        image_ranker_app.last_shown_image = None
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
        self.assertIn(
            image_ranker_app.canonicalize_pair((self.image_paths[0], self.image_paths[2])),
            image_ranker_app.skipped_pairs,
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

    def test_set_directory_accepts_missing_autosave_field(self):
        response = self.client.post(
            "/set_directory",
            data={"path": "dataset"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(image_ranker_app.current_directory, self.dataset_dir)

    def test_load_exclusions_from_autosave_ignores_malformed_json(self):
        exclusions_file = image_ranker_app.get_exclusions_file_path(self.autosave_file)
        with open(exclusions_file, "w") as f:
            f.write("{not valid json")

        self.assertEqual(
            image_ranker_app.load_exclusions_from_autosave(self.autosave_file),
            {},
        )

    def test_import_comparison_history_rejects_rankings_csv(self):
        rankings_csv = io.BytesIO(
            b"Image,ELO,Uncertainty,Upvotes,Downvotes\nimg_a,1,2,3,4\n"
        )

        response = self.client.post(
            "/import_comparison_history",
            data={
                "file": (rankings_csv, "image_rankings.csv"),
                "append": "false",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("comparisons.csv", response.get_json()["error"])

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
        self.assertIn(
            image_ranker_app.canonicalize_pair((self.image_paths[0], self.image_paths[2])),
            image_ranker_app.skipped_pairs,
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

    def test_skip_pair_removes_pair_from_queue_and_rebuilds_do_not_restore_it(self):
        self.client.post(
            "/set_directory",
            data={"path": "dataset"},
        )

        skipped_pair = image_ranker_app.image_pairs[0]
        image_ranker_app.current_pair_index = 1

        response = self.client.post("/skip_pair")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(image_ranker_app.current_pair_index, 0)
        self.assertNotIn(skipped_pair, image_ranker_app.image_pairs)
        self.assertIn(
            image_ranker_app.canonicalize_pair(skipped_pair),
            image_ranker_app.skipped_pairs,
        )

        image_ranker_app.initialize_image_pairs()

        self.assertNotIn(
            image_ranker_app.canonicalize_pair(skipped_pair),
            {image_ranker_app.canonicalize_pair(pair) for pair in image_ranker_app.image_pairs},
        )


if __name__ == "__main__":
    unittest.main()
