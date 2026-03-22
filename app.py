from flask import Flask, render_template, request, jsonify, send_file, Response, abort, render_template_string, url_for
import os
import random
import itertools
from elo import TrueSkillRanking
import csv
from io import StringIO
import threading
from threading import Thread
import time
from datetime import datetime
import logging
import json
import sys
import subprocess

logging.basicConfig(level=logging.DEBUG)

# Global variables
app = Flask(__name__)
elo_ranking = TrueSkillRanking()
excluded_images = {}
exclusion_reasons = None
IMAGE_FOLDER = 'static/images'
image_pairs_lock = threading.Lock()
comparisons_autosave_prefix = 'comparisons_autosave_'
AUTOSAVE_FREQUENCY = int(os.environ.get('AUTOSAVE_FREQUENCY', '10'))
SOUND_ENABLED = os.environ.get('SOUND_ENABLED', 'True').lower() == 'true'
BASE_DIR = os.environ.get('BASE_DIR')

exclusion_reasons_file = os.environ.get('EXCLUSION_REASONS_FILE')
if exclusion_reasons_file:
    try:
        with open(exclusion_reasons_file, 'r') as f:
            exclusion_reasons = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(f"Error loading exclusion reasons file: {e}")

current_directory = None

image_pairs = []
skipped_pairs = set()
current_pair_index = 0
last_shown_image = None
current_displayed_pair = None
context_data = None
SUPPORTED_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.jfif', '.avif', '.heic', '.heif')
directory_status = {
    'state': 'no_directory',
    'message': 'Select a folder containing at least 2 supported images to begin.',
    'image_count': 0,
}

comparisons_since_autosave = 0


def get_default_demo_directory():
    if os.path.isabs(IMAGE_FOLDER):
        return IMAGE_FOLDER
    return os.path.abspath(IMAGE_FOLDER)


def initialize_default_demo_directory():
    global current_directory, IMAGE_FOLDER

    if current_directory:
        return

    default_directory = get_default_demo_directory()
    if not os.path.isdir(default_directory):
        return

    IMAGE_FOLDER = default_directory
    current_directory = default_directory
    reset_ranking_session()
    load_context_for_directory(default_directory)
    initialize_image_pairs()


def get_restriction_root():
    base_dir = BASE_DIR or os.environ.get('BASE_DIR')
    if not base_dir:
        return None
    return os.path.realpath(os.path.abspath(os.path.expanduser(base_dir)))


def get_browse_root():
    return get_restriction_root() or os.path.expanduser('~')


def is_within_root(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def resolve_user_path(raw_path, fallback_root=None):
    if raw_path is None:
        raise ValueError('No directory selected')

    candidate = raw_path.strip()
    if not candidate:
        raise ValueError('No directory selected')

    candidate = os.path.expanduser(candidate)
    if os.path.isabs(candidate):
        resolved = os.path.realpath(os.path.abspath(candidate))
    else:
        base_root = fallback_root or get_browse_root()
        resolved = os.path.realpath(os.path.abspath(os.path.join(base_root, candidate)))

    restriction_root = get_restriction_root()
    if restriction_root and not is_within_root(resolved, restriction_root):
        raise PermissionError(f'Selected path must stay inside BASE_DIR: {restriction_root}')

    return resolved


def update_directory_status(image_count, state=None, message=None):
    global directory_status, current_directory

    if state is None:
        if image_count == 0:
            state = 'empty'
            message = (
                'No supported images found in the selected folder. '
                f'Supported formats: {", ".join(ext.lstrip(".") for ext in SUPPORTED_IMAGE_EXTENSIONS)}.'
            )
        elif image_count == 1:
            state = 'insufficient'
            message = 'Found 1 supported image. Add at least one more image to start ranking.'
        else:
            state = 'ready'
            message = f'Found {image_count} supported images. Ranking is ready.'

    directory_status = {
        'state': state,
        'message': message,
        'image_count': image_count,
        'directory': current_directory,
    }


def describe_path_access_error(path, error):
    base_message = f'Cannot access "{path}": {error}.'
    if sys.platform == 'darwin':
        return (
            f'{base_message} On macOS, Python may need explicit permission for Desktop, Documents, or Downloads. '
            'Grant access to your terminal app in System Settings > Privacy & Security > Files and Folders, then retry.'
        )
    return base_message


@app.route('/open_macos_privacy_settings', methods=['POST'])
def open_macos_privacy_settings():
    if sys.platform != 'darwin':
        return jsonify({'success': False, 'error': 'This action is only available on macOS.'}), 400

    try:
        subprocess.run(
            ['open', 'x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders'],
            check=True,
        )
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Failed to open macOS privacy settings: {e}")
        return jsonify({'success': False, 'error': 'Failed to open macOS privacy settings.'}), 500


def get_exclusions_file_path(autosave_file):
    autosave_date = os.path.basename(autosave_file).split("_")[-1].replace(".csv", "")
    return os.path.join(os.path.dirname(autosave_file), f'exclusions_autosave_{autosave_date}.json')


def get_skipped_pairs_file_path(autosave_file):
    autosave_date = os.path.basename(autosave_file).split("_")[-1].replace(".csv", "")
    return os.path.join(os.path.dirname(autosave_file), f'skipped_pairs_autosave_{autosave_date}.json')


def load_exclusions_from_autosave(autosave_file):
    exclusions_file_path = get_exclusions_file_path(autosave_file)
    if not os.path.exists(exclusions_file_path):
        return {}

    try:
        with open(exclusions_file_path, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        app.logger.error(f"Failed to load exclusions file {exclusions_file_path}: {e}")
        return {}


def canonicalize_pair(pair):
    return tuple(sorted(pair))


def load_skipped_pairs_from_autosave(autosave_file):
    skipped_pairs_file_path = get_skipped_pairs_file_path(autosave_file)
    if not os.path.exists(skipped_pairs_file_path):
        return set()

    try:
        with open(skipped_pairs_file_path, 'r') as f:
            rows = json.load(f)
        return {
            canonicalize_pair((row[0], row[1]))
            for row in rows
            if isinstance(row, list) and len(row) == 2 and all(isinstance(value, str) for value in row)
        }
    except (OSError, json.JSONDecodeError, TypeError) as e:
        app.logger.error(f"Failed to load skipped pairs file {skipped_pairs_file_path}: {e}")
        return set()


def reset_ranking_session(load_context=False):
    global elo_ranking, excluded_images, image_pairs, skipped_pairs, current_pair_index
    global comparisons_since_autosave, context_data, last_shown_image, current_displayed_pair

    elo_ranking = TrueSkillRanking()
    excluded_images = {}
    image_pairs = []
    skipped_pairs = set()
    current_pair_index = 0
    last_shown_image = None
    current_displayed_pair = None
    comparisons_since_autosave = 0
    if not load_context:
        context_data = None


def load_context_for_directory(directory):
    global context_data

    context_data = None
    context_json_path = os.path.join(directory, 'context.json')
    context_txt_path = os.path.join(directory, 'context.txt')
    if os.path.exists(context_json_path):
        with open(context_json_path, 'r') as f:
            try:
                context_data = json.load(f)
            except json.JSONDecodeError:
                app.logger.error(f"Failed to decode {context_json_path}")
                context_data = {'error': 'Failed to decode context.json'}
    elif os.path.exists(context_txt_path):
        with open(context_txt_path, 'r') as f:
            context_data = f.read()


def maybe_load_current_directory_autosave_exclusions(filename):
    global excluded_images

    if not current_directory or not filename:
        return False

    basename = os.path.basename(filename)
    if not basename.startswith(comparisons_autosave_prefix):
        return False

    autosave_file = os.path.join(current_directory, basename)
    if os.path.exists(autosave_file):
        excluded_images = load_exclusions_from_autosave(autosave_file)
        return True

    return False


def maybe_load_current_directory_autosave_skipped_pairs(filename):
    global skipped_pairs

    if not current_directory or not filename:
        return False

    basename = os.path.basename(filename)
    if not basename.startswith(comparisons_autosave_prefix):
        return False

    autosave_file = os.path.join(current_directory, basename)
    if os.path.exists(autosave_file):
        skipped_pairs = load_skipped_pairs_from_autosave(autosave_file)
        return True

    return False

def get_image_paths(folder, timeout=None, start_time=None, get_progress=False, return_metadata=False):
    global comparisons_autosave_prefix
    image_paths = []
    comparison_progress = 0
    has_progress_file = False
    for root, dirs, files in os.walk(folder):
        if timeout is not None and start_time is not None:
            if time.time() - start_time > timeout:
                if return_metadata:
                    return [], None, {'has_progress_file': False}
                return [], None # Don't return incomplete list/incorrect progress count
        comparison_progress_file = None
        for file in files:
            if is_eligible_image(root, file):
                image_paths.append(os.path.join(root, file).replace('\\', '/'))
            elif (get_progress and file.startswith(comparisons_autosave_prefix) and
                 (comparison_progress_file is None or file > comparison_progress_file)):
                comparison_progress_file = file
        if get_progress and comparison_progress_file:
            has_progress_file = True
            newline_count = count_newlines_in_file(os.path.join(root, comparison_progress_file))
            comparison_progress += max(newline_count - 1, 0) # Ignore header
    if return_metadata:
        return image_paths, comparison_progress, {'has_progress_file': has_progress_file}
    return image_paths, comparison_progress

def is_eligible_image(root, file):
    if file.lower().endswith(SUPPORTED_IMAGE_EXTENSIONS):
        image_path = os.path.join(root, file).replace('\\', '/')
        if image_path not in excluded_images:
            return True
    return False

def count_newlines_in_file(file):
    count = 0
    with open(file, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            count += chunk.count(b'\n')
    return count

def get_image_counts_in_folders(folders, timeout=0.5):
    start_time = time.time()
    results = []
    total_image_count = 0
    timed_out = False
    for folder in folders:
        image_count = None
        comparison_progress = None
        has_progress_file = False
        try:
            if time.time() - start_time < timeout:
                paths, comparison_progress, metadata = get_image_paths(
                    folder,
                    timeout=timeout,
                    start_time=start_time,
                    get_progress=True,
                    return_metadata=True,
                )
                image_count = len(paths)
                has_progress_file = metadata['has_progress_file']
            else: 
                timed_out = True
        except OSError:
            image_count = None
            comparison_progress = None
            has_progress_file = False
        folder_name = os.path.basename(os.path.normpath(folder))
        results.append({
            'folder': folder_name,
            'path': folder,
            'image_count': image_count,
            'comparison_progress': comparison_progress,
            'has_progress_file': has_progress_file,
        })
        total_image_count += image_count or 0
    return results, total_image_count, timed_out


def get_browse_folder_sort_key(folder):
    image_count = folder['image_count']
    comparison_progress = folder['comparison_progress'] or 0
    has_progress_file = folder.get('has_progress_file', False)
    is_ready = image_count is not None and image_count >= 2
    has_images = image_count is not None and image_count > 0
    image_count_value = image_count if image_count is not None else -1

    return (
        0 if has_progress_file else 1,
        0 if is_ready else 1,
        0 if has_images else 1,
        -comparison_progress,
        -image_count_value,
        folder['folder'].lower(),
    )


def sort_browse_folders(folders, sort_mode):
    if sort_mode == 'name':
        return sorted(folders, key=lambda folder: folder['folder'].lower())

    if sort_mode == 'images':
        return sorted(
            folders,
            key=lambda folder: (
                -(folder['image_count'] if folder['image_count'] is not None else -1),
                -int(bool(folder.get('has_progress_file', False))),
                -(folder['comparison_progress'] or 0),
                folder['folder'].lower(),
            ),
        )

    if sort_mode == 'progress':
        return sorted(
            folders,
            key=lambda folder: (
                -int(bool(folder.get('has_progress_file', False))),
                -(folder['comparison_progress'] or 0),
                -(folder['image_count'] if folder['image_count'] is not None else -1),
                folder['folder'].lower(),
            ),
        )

    return sorted(folders, key=get_browse_folder_sort_key)

def initialize_image_pairs(a=False):
    global image_pairs, current_pair_index, current_displayed_pair
    image_paths, _ = get_image_paths(IMAGE_FOLDER)
    update_directory_status(len(image_paths))
    
    if len(image_paths) < 2:
        image_pairs = []
        current_pair_index = 0
        current_displayed_pair = None
        return

    random.shuffle(image_paths)
    n = len(image_paths)
    initial_pairs = []
    for i in range(n):
        pair = (image_paths[i], image_paths[(i+1) % n])
        if (pair[1], pair[0]) not in initial_pairs:
            initial_pairs.append(pair)
    
    app.logger.debug(f"Created {len(initial_pairs)} initial pairs")
    
    random.shuffle(initial_pairs)
    remaining_pairs = list(itertools.combinations(image_paths, 2))
    
    initial_pairs_set = set(initial_pairs) | set((p[1], p[0]) for p in initial_pairs)
    remaining_pairs = [pair for pair in remaining_pairs if pair not in initial_pairs_set]
    
    app.logger.debug(f"Created {len(remaining_pairs)} remaining pairs")
    
    image_pairs = initial_pairs + remaining_pairs
    image_pairs = [
        pair for pair in image_pairs
        if pair[0] not in excluded_images
        and pair[1] not in excluded_images
        and canonicalize_pair(pair) not in skipped_pairs
    ]
    
    app.logger.info(f"Total pairs created: {len(image_pairs)}")
    
    random.shuffle(image_pairs[n:])
    current_pair_index = 0
    current_displayed_pair = None


def requeue_pair_for_reranking(pair):
    global image_pairs, current_pair_index, current_displayed_pair

    target_index = max(current_pair_index - (1 if current_displayed_pair is not None else 0), 0)
    target_pair = canonicalize_pair(pair)
    rebuilt_pairs = []

    for existing_pair in image_pairs:
        if canonicalize_pair(existing_pair) == target_pair:
            if len(rebuilt_pairs) < target_index:
                target_index -= 1
            continue
        rebuilt_pairs.append(existing_pair)

    rebuilt_pairs.insert(target_index, pair)
    image_pairs = rebuilt_pairs
    current_pair_index = target_index
    current_displayed_pair = None


def parse_comparison_history_rows(file):
    if hasattr(file, 'seek'):
        file.seek(0)

    rows = list(csv.reader(file.read().decode('utf-8-sig').splitlines()))
    if not rows:
        raise ValueError(
            "The selected file is empty. Import comparisons.csv or comparisons_autosave_YYYY-MM-DD.csv."
        )

    header = [column.strip() for column in rows[0]]
    if header != ['Winner', 'Loser']:
        raise ValueError(
            "Import comparisons.csv or comparisons_autosave_YYYY-MM-DD.csv. "
            "Ranking CSV files and exclusions JSON files cannot restore a session."
        )

    return rows[1:]


def import_comparison_history_file(file, append):
    global image_pairs, excluded_images, skipped_pairs

    rows = parse_comparison_history_rows(file)

    if not append:
        preserved_exclusions = excluded_images.copy()
        preserved_skipped_pairs = skipped_pairs.copy()
        reset_ranking_session(load_context=True)
        if current_directory:
            restored_autosave_exclusions = maybe_load_current_directory_autosave_exclusions(
                getattr(file, 'filename', None)
            )
            restored_autosave_skipped_pairs = maybe_load_current_directory_autosave_skipped_pairs(
                getattr(file, 'filename', None)
            )
            if not restored_autosave_exclusions:
                excluded_images = preserved_exclusions
            if not restored_autosave_skipped_pairs:
                skipped_pairs = preserved_skipped_pairs
            initialize_image_pairs()

    pairs_to_add = set()
    losers_to_remove = set()
    pairs_to_remove = set()
    for row in rows:
        winner, loser = row
        if winner == 'None':  # Handle cases where winner is None
            losers_to_remove.add(loser)
        else:
            pairs_to_add.add((winner, loser))
        # Collect pairs to remove
        pairs_to_remove.add((winner, loser))
        pairs_to_remove.add((loser, winner))

    # Remove losers from image_pairs and elo_ranking
    image_pairs = [(img1, img2) for img1, img2 in image_pairs if img1 not in losers_to_remove and img2 not in losers_to_remove]
    elo_ranking.update_rating(pairs_to_add)
    elo_ranking.remove_image(losers_to_remove)
    
    # Remove duplicate pairs from image_pairs
    image_pairs = [(img1, img2) for img1, img2 in image_pairs if (img1, img2) not in pairs_to_remove]

@app.route('/')
def index():
    initialize_default_demo_directory()
    return render_template('index.html', sound_enabled=SOUND_ENABLED)

def smart_shuffle():
    """
    Reorders the image pairs based on their ELO ratings and comparison counts.

    This function removes the image pairs that have already been compared, 
    retrieves the current ELO rankings and comparison counts, and then 
    sorts the remaining image pairs based on their ELO differences and 
    comparison counts. The image pairs with the smallest ELO differences 
    and comparison counts are placed first in the list.
    """
    global image_pairs
    global current_pair_index
    
    with image_pairs_lock:
        image_pairs = image_pairs[current_pair_index:]
        current_pair_index = 0
        rankings = elo_ranking.get_rankings()
        elo_dict = {image: rating.mu for image, rating in rankings}
        count_dict = {image: elo_ranking.counts.get(image, 0) for image in elo_dict}
        
        def get_elo_difference(pair):
            return abs(elo_dict.get(pair[0], 0) - elo_dict.get(pair[1], 0)) + 0.8 * (count_dict.get(pair[0], 0) + count_dict.get(pair[1], 0))
        
        image_pairs.sort(key=get_elo_difference)
        
@app.route('/smart_shuffle')
def smart_shuffle_route():
    try:
        smart_shuffle()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/get_images')
def get_images():
    global current_pair_index, last_shown_image, current_directory, image_pairs, directory_status
    global current_displayed_pair
    if not current_directory:
        return jsonify({
            'state': 'no_directory',
            'message': 'Select a folder containing at least 2 supported images to begin.',
            'image_count': 0,
        }), 200

    if directory_status['state'] != 'ready':
        return jsonify({
            'state': directory_status['state'],
            'message': directory_status['message'],
            'image_count': directory_status['image_count'],
        }), 200

    with image_pairs_lock:
        if current_pair_index >= len(image_pairs):
            current_displayed_pair = None
            completed_pairs = len(elo_ranking.comparison_history)
            return jsonify({
                'state': 'completed',
                'message': 'All comparisons for this folder are complete.',
                'progress': {
                    'current': completed_pairs,
                    'total': completed_pairs
                }
            })
        
        queued_pair = image_pairs[current_pair_index]
        img1, img2 = queued_pair
        if last_shown_image is not None:
            if img1 == last_shown_image:
                img1, img2 = img2, img1
                app.logger.debug(f"Swapped display order: {os.path.basename(img1)} vs {os.path.basename(img2)}")
            elif img2 == last_shown_image:
                pass
        current_displayed_pair = queued_pair
        last_shown_image = img1
        current_pair_index += 1
        completed_pairs = len(elo_ranking.comparison_history)
        total_pairs = len(image_pairs) + completed_pairs - current_pair_index + 1
    return jsonify({
        'image1':  img1,
        'image2':  img2,
        'progress': {
            'current': completed_pairs,
            'total': total_pairs
        }
    })

@app.route('/serve_image')
def serve_image():
    try:
        image_path = request.args.get('path')
        if not image_path:
            return jsonify({'error': 'No image path provided'}), 400
            
        # Remove any URL encoding from the path
        image_path = os.path.normpath(image_path)
        
        # If the path is relative to IMAGE_FOLDER, make it absolute
        if not os.path.isabs(image_path):
            image_path = os.path.join(IMAGE_FOLDER, os.path.basename(image_path))
        
        app.logger.debug(f"Attempting to serve image: {image_path}")
        
        # Check if the file exists
        if not os.path.exists(image_path):
            app.logger.error(f"Image not found: {image_path}")
            return jsonify({'error': 'Image not found'}), 404
            
        file_extension = os.path.splitext(image_path)[1].lower()
        if file_extension == '.webp':
            mimetype = 'image/webp'
        elif file_extension in ['.jpg', '.jpeg']:
            mimetype = 'image/jpeg'
        elif file_extension == '.png':
            mimetype = 'image/png'
        elif file_extension == '.gif':
            mimetype = 'image/gif'
        else:
            mimetype = 'image/jpeg'  # default
            
        app.logger.debug(f"Serving image with mimetype: {mimetype}")
        return send_file(image_path, mimetype=mimetype)
    except Exception as e:
        app.logger.error(f"Error serving image: {str(e)}")
        return jsonify({'error': str(e)}), 500

def autosave_rankings():
    global elo_ranking, current_directory, comparisons_autosave_prefix, excluded_images, skipped_pairs
    
    if not current_directory:
        app.logger.warning("No image directory selected. Autosave aborted.")
        return

    # Get current date
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    # Save rankings
    rankings = elo_ranking.get_rankings()
    rankings_filename = os.path.join(current_directory, f'image_rankings_autosave_{current_date}.csv')
    with open(rankings_filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Image', 'ELO', 'Uncertainty', 'Upvotes', 'Downvotes'])
        for image, rating in rankings:
            writer.writerow([
                image,
                round(rating.mu, 2),
                round(rating.sigma, 2),
                elo_ranking.upvotes.get(image, 0),
                elo_ranking.downvotes.get(image, 0)
            ])
    
    # Save comparisons
    comparisons = elo_ranking.comparison_history
    comparisons_filename = os.path.join(current_directory, f'{comparisons_autosave_prefix}{current_date}.csv')
    with open(comparisons_filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Winner', 'Loser'])
        for winner, loser in comparisons:
            if winner is None:
                writer.writerow(['None', loser])
            else:
                writer.writerow([winner, loser])

    # Save exclusions
    exclusions_filename = os.path.join(current_directory, f'exclusions_autosave_{current_date}.json')
    with open(exclusions_filename, 'w') as f:
        json.dump(excluded_images, f)

    skipped_pairs_filename = os.path.join(current_directory, f'skipped_pairs_autosave_{current_date}.json')
    with open(skipped_pairs_filename, 'w') as f:
        json.dump([list(pair) for pair in sorted(skipped_pairs)], f)

    app.logger.info(
        f"Autosave completed. Files saved in {current_directory}: "
        f"{os.path.basename(rankings_filename)}, {os.path.basename(comparisons_filename)}, "
        f"{os.path.basename(exclusions_filename)}, {os.path.basename(skipped_pairs_filename)}"
    )

@app.route('/update_elo', methods=['POST'])
def update_elo():
    global comparisons_since_autosave, current_pair_index, current_displayed_pair
    data = request.json
    if not data or 'winner' not in data or 'loser' not in data:
        return jsonify({'error': 'Missing winner or loser in request'}), 400
    winner = data['winner']
    loser = data['loser']
    elo_ranking.update_rating((winner, loser))
    current_displayed_pair = None
    if data.get('exclude_loser', False):
        excluded_images[loser] = 'excluded'
        # Recalculate image pairs
        initialize_image_pairs()
    
    # Increment the counter and check if it's time to autosave
    comparisons_since_autosave += 1
    if comparisons_since_autosave >= AUTOSAVE_FREQUENCY or current_pair_index >= len(image_pairs):
        autosave_rankings()
        comparisons_since_autosave = 0
    
    return jsonify({'success': True})

@app.route('/skip_pair', methods=['POST'])
def skip_pair():
    global current_pair_index, image_pairs, current_directory, skipped_pairs, current_displayed_pair
    if not current_directory:
        return jsonify({'error': 'No directory selected'}), 400

    skipped = False
    with image_pairs_lock:
        if current_pair_index > 0 and current_pair_index <= len(image_pairs):
            pair = image_pairs.pop(current_pair_index - 1)
            skipped_pairs.add(canonicalize_pair(pair))
            current_pair_index -= 1
            current_displayed_pair = None
            app.logger.info(f"Skipped pair: {pair}")
            skipped = True

    if not skipped:
        return jsonify({'error': 'No pair to skip'}), 400

    autosave_rankings()
    return jsonify({'success': True})

@app.route('/remove_image', methods=['POST'])
def remove_image():
    global current_displayed_pair
    image = request.json['del_img']
    global image_pairs
    image_pairs = [(img1, img2) for img1, img2 in image_pairs if img1!= image and img2!= image]
    elo_ranking.remove_image(image)
    current_displayed_pair = None
    return jsonify({'success': True})


@app.route('/revert_last_comparison', methods=['POST'])
def revert_last_comparison():
    global comparisons_since_autosave, last_shown_image

    if not current_directory:
        return jsonify({'error': 'No directory selected'}), 400

    with image_pairs_lock:
        reverted_pair = elo_ranking.revert_last_comparison()
        if reverted_pair is None:
            return jsonify({'error': 'No ranking decision is available to revert.'}), 400

        requeue_pair_for_reranking(reverted_pair)
        last_shown_image = None

    comparisons_since_autosave = 0
    autosave_rankings()
    return jsonify({'success': True, 'pair': list(reverted_pair)})

@app.route('/get_rankings')
def get_rankings():
    try:
        rankings = elo_ranking.get_rankings()
        return jsonify([
            {
                'image': image,
                'elo': rating.mu,
                'uncertainty': rating.sigma,
                'count': elo_ranking.counts.get(image, 0),
                'upvotes': elo_ranking.upvotes.get(image, 0),
                'downvotes': elo_ranking.downvotes.get(image, 0),
                'excluded': image in excluded_images
            }
            for image, rating in rankings
        ])
    except Exception as e:
        app.logger.error(f"Error in get_rankings: {str(e)}.")
        return jsonify({'error': str(e)}), 500

@app.route('/get_progress')
def get_progress():
    return jsonify({
        'current': current_pair_index,
        'total': len(image_pairs)
    })

@app.route('/set_directory', methods=['POST'])
def set_directory():
    global IMAGE_FOLDER, current_directory, elo_ranking, image_pairs, current_pair_index, comparisons_since_autosave, excluded_images, context_data
    
    try:
        selected_path = request.form["path"]
        selected_autosave_path = request.form.get("autosaveFile", "")

        directory = resolve_user_path(selected_path)

        if directory:
            if not os.path.exists(directory):
                return jsonify({'success': False, 'error': f'Directory does not exist: {directory}'}), 400
            if not os.path.isdir(directory):
                return jsonify({'success': False, 'error': f'Not a directory: {directory}'}), 400
            try:
                os.listdir(directory)
            except OSError as e:
                return jsonify({'success': False, 'error': describe_path_access_error(directory, e)}), 403

            IMAGE_FOLDER = directory
            current_directory = directory  # Save the selected directory
            reset_ranking_session()
            load_context_for_directory(directory)

            autosave_file = None
            if selected_autosave_path:
                autosave_file = resolve_user_path(selected_autosave_path)
                if os.path.exists(autosave_file):
                    excluded_images.update(load_exclusions_from_autosave(autosave_file))
                    skipped_pairs.update(load_skipped_pairs_from_autosave(autosave_file))

            initialize_image_pairs()

            if autosave_file and os.path.exists(autosave_file):
                with open(autosave_file, 'rb') as file:
                    import_comparison_history_file(file, True)

            return jsonify({
                'success': True,
                'directory': directory,
                'state': directory_status['state'],
                'message': directory_status['message'],
                'image_count': directory_status['image_count'],
            })
        else:
            return jsonify({'success': False, 'error': 'No directory selected'})
    except PermissionError as e:
        return jsonify({'success': False, 'error': str(e)}), 403
    except ValueError as e:
        app.logger.error(f"Error in set_directory: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        app.logger.error(f"Error in set_directory: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/export_rankings')
def export_rankings():
    app.logger.info("Export rankings route called.")
    try:
        rankings = elo_ranking.get_rankings()
        app.logger.info(f"Rankings: {rankings}")
        if not rankings:
            app.logger.warning("No rankings data available.")
            return jsonify({'error': 'No rankings data available. Please make some comparisons first.'}), 400

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Image', 'ELO', 'Uncertainty', 'Upvotes', 'Downvotes'])
        for image, rating in rankings:
            writer.writerow([
                image,
                round(rating.mu, 2),
                round(rating.sigma, 2),
                elo_ranking.upvotes.get(image, 0),
                elo_ranking.downvotes.get(image, 0)
            ])
        
        output.seek(0)
        app.logger.info("CSV data created successfully.")
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={"Content-disposition": "attachment; filename=image_rankings.csv"}
        )
    except Exception as e:
        app.logger.error(f"Error in export_rankings: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/export_comparisons')
def export_comparisons():
    app.logger.info("Export comparisons route called.")
    try:
        comparisons = elo_ranking.comparison_history
        app.logger.info(f"Comparisons: {comparisons}")
        if not comparisons:
            app.logger.warning("No comparisons data available.")
            return jsonify({'error': 'No comparisons data available. Please make some comparisons first.'}), 400

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Winner', 'Loser'])
        for winner, loser in comparisons:
            if winner is None:
                writer.writerow(['None', loser])
            else:
                writer.writerow([winner, loser])
        
        output.seek(0)
        app.logger.info("CSV data created successfully.")
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={"Content-disposition": "attachment; filename=comparisons.csv"}
        )
    except Exception as e:
        app.logger.error(f"Error in export_comparisons: {str(e)}.")
        return jsonify({'error': str(e)}), 500

@app.route('/export_exclusions')
def export_exclusions():
    app.logger.info("Export exclusions route called.")
    try:
        if not excluded_images:
            app.logger.warning("No exclusions data available.")
            return jsonify({'error': 'No exclusions data available.'}), 400

        return jsonify(excluded_images)
    except Exception as e:
        app.logger.error(f"Error in export_exclusions: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/import_comparison_history', methods=['POST'])
def import_comparison_history():
    file = request.files['file']
    append = request.form.get('append', 'false') == 'true'

    try:
        import_comparison_history_file(file, append)
        return jsonify({'success': True})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/get_exclusion_reasons')
def get_exclusion_reasons():
    global exclusion_reasons
    return jsonify(exclusion_reasons)

@app.route('/exclude_image', methods=['POST'])
def exclude_image():
    global excluded_images, current_displayed_pair
    data = request.json
    excluded_image = data['excluded_image']
    reason = data.get('reason', 'excluded')
    excluded_images[excluded_image] = reason
    # Recalculate image pairs
    initialize_image_pairs()
    current_displayed_pair = None
    return jsonify({'success': True})

@app.route('/clear_excluded_images', methods=['POST'])
def clear_excluded_images():
    global excluded_images, current_displayed_pair
    excluded_images.clear()
    # Recalculate image pairs
    initialize_image_pairs()
    current_displayed_pair = None
    return jsonify({'success': True})

# Add a new route to get the current directory
@app.route('/get_current_directory')
def get_current_directory():
    global current_directory
    return jsonify({
        'directory': current_directory if current_directory else None,
        'state': directory_status['state'],
        'message': directory_status['message'],
        'image_count': directory_status['image_count'],
    })

@app.route("/browse_directory")
def browse_directory():
    browse_root = get_browse_root()
    requested_path = request.args.get("path", "")
    sort_mode = request.args.get("sort", "smart").lower()
    if sort_mode not in {'smart', 'progress', 'images', 'name'}:
        sort_mode = 'smart'
    try:
        abs_path = resolve_user_path(requested_path, fallback_root=browse_root) if requested_path else browse_root
    except PermissionError:
        abort(403)
    except ValueError:
        abs_path = browse_root

    if not os.path.isdir(abs_path):
        abort(404)

    try:
        all_files = os.listdir(abs_path)
    except OSError as e:
        return render_template(
            "browse-dir.html",
            folders=[],
            current_path=abs_path,
            parent_path=os.path.dirname(abs_path) if abs_path != os.path.dirname(abs_path) and (not get_restriction_root() or abs_path != get_restriction_root()) else None,
            images_in_current_folder=None,
            autosave_progress_file=None,
            browse_root=browse_root,
            restriction_root=get_restriction_root(),
            supported_extensions=', '.join(ext.lstrip('.') for ext in SUPPORTED_IMAGE_EXTENSIONS),
            error_message=describe_path_access_error(abs_path, e),
            show_mac_privacy=sys.platform == 'darwin',
            current_sort=sort_mode,
        )
    folders = [
        d for d in all_files
        if os.path.isdir(os.path.join(abs_path, d))
    ]
    autosave_progress_files = [f for f in all_files if f.startswith(comparisons_autosave_prefix)]
    autosave_progress_file = None
    if len(autosave_progress_files):
        autosave_progress_file = os.path.join(abs_path, max(autosave_progress_files))
    if len(folders):
        folders, total_image_count, timed_out = get_image_counts_in_folders([os.path.join(abs_path, folder) for folder in folders])
        folders = sort_browse_folders(folders, sort_mode)
        if timed_out:
            total_image_count = str(total_image_count) + '+'
    else:
        total_image_count = len([f for f in all_files if is_eligible_image(abs_path, f)])
    return render_template(
        "browse-dir.html",
        folders=folders,
        current_path=abs_path,
        parent_path=os.path.dirname(abs_path) if abs_path != os.path.dirname(abs_path) and (not get_restriction_root() or abs_path != get_restriction_root()) else None,
        images_in_current_folder=total_image_count,
        autosave_progress_file=autosave_progress_file,
        browse_root=browse_root,
        restriction_root=get_restriction_root(),
        supported_extensions=', '.join(ext.lstrip('.') for ext in SUPPORTED_IMAGE_EXTENSIONS),
        show_mac_privacy=sys.platform == 'darwin',
        current_sort=sort_mode,
    )


@app.route("/context_exists")
def context_exists():
    global context_data
    image_path = request.args.get("path")
    if not image_path:
        return jsonify({"exists": False})

    if context_data is None:
        return jsonify({"exists": False})

    if isinstance(context_data, str):
        return jsonify({"exists": True})

    filename = os.path.basename(image_path)
    if filename in context_data or 'default' in context_data:
        return jsonify({"exists": True})

    return jsonify({"exists": False})


@app.route("/get_context")
def get_context():
    global context_data
    image_path = request.args.get("path")
    if not image_path:
        abort(400, "Image path is required.")

    if context_data is None:
        abort(404, "Context file not found.")

    if isinstance(context_data, str):
        return context_data

    if 'error' in context_data:
        abort(500, context_data['error'])

    filename = os.path.basename(image_path)
    context_html = None
    if filename in context_data:
        context_html = context_data[filename]
    elif 'default' in context_data:
        context_html = context_data['default']

    if context_html:
        return render_template_string(context_html, url_for=url_for)
    else:
        abort(404, f"Context not found for {filename} and no default is set.")

def main():
    initialize_default_demo_directory()
    global comparisons_since_autosave
    comparisons_since_autosave = 0
    app.run(debug=False, threaded=True)


if __name__ == '__main__':
    main()
