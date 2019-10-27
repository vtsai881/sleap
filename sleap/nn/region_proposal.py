"""This module contains utilities for generating and working with region proposals.

Region proposals are used to extract crops from a larger image for downstream
processing. This is a technique that can drastically improve performance and memory
usage when the foreground region occupies a small fraction of the image.
"""

import attr
from typing import Tuple, List
import itertools
from collections import defaultdict
import numpy as np
import tensorflow as tf
from sleap.nn import peak_finding
from sleap.nn import utils
from sleap.nn.inference import InferenceModel


@attr.s(auto_attribs=True, slots=True)
class RegionProposalSet:
    box_size: Tuple[int, int]
    sample_inds: np.ndarray
    bboxes: np.ndarray
    patches: tf.Tensor


def make_centered_bboxes(
    centroids: np.ndarray, box_length: int, center_offset: bool = True
) -> np.ndarray:
    """Generates bounding boxes centered on a set of centroid coordinates.

    This function creates fixed size bounding boxes centered on the centroids to
    be used as region proposals.

    Args:
        centroids: Numpy array of shape (n_peaks, 4) where subscripts of centroid
            locations are specified in each row as [sample, row, col, channel], or
            of shape (n_peaks, 2) where subscripts are specified as [row, col].
        box_length: A scalar integer that specifies the width and height of the
            bounding boxes centered at each centroid location.
        center_offset: If True, add 0.5 to coordinates to adjust for integer peak
            subscripts. Set this to True when going from grid subscripts to real-valued
            image coordinates in order to offset to the center rather than the top-left
            corner of each pixel.

    Returns:
        bboxes a numpy array of shape (n_peaks, 4) specifying the bounding boxes.

        Bounding boxes are specified in the format [y1, x1, y2, x2], where the
        coordinates correspond to the top-left (y1, x1) and bottom-right (y2, x2) of
        each bounding box in absolute image coordinates.
    """

    # Pull out peak subscripts.
    if centroids.shape[1] == 2:
        centroids_y, centroids_x = np.split(centroids, 2, axis=1)

    elif centroids.shape[1] == 4:
        _, centroids_y, centroids_x, _ = np.split(centroids, 4, axis=1)

    # Initialize with centroid locations.
    bboxes = np.concatenate(
        [centroids_y, centroids_x, centroids_y, centroids_x], axis=1
    )

    # Offset by half of the box length in each direction.
    bboxes += np.array(
        [
            [
                -box_length // 2,  # top
                -box_length // 2,  # left
                box_length // 2,  # bottom
                box_length // 2,  # right
            ]
        ]
    )

    # Adjust to center of the pixel.
    if center_offset:
        bboxes += 0.5

    return bboxes


def compute_iou(bbox1: np.ndarray, bbox2: np.ndarray) -> float:
    """Computes the intersection over union for a pair of bounding boxes.

    Args:
        bbox1: Bounding box specified by corner coordinates [y1, x1, y2, x2].
        bbox2: Bounding box specified by corner coordinates [y1, x1, y2, x2].

    Returns:
        A float scalar calculated as the ratio between the areas of the intersection
        and the union of the two bounding boxes.
    """

    bbox1_y1, bbox1_x1, bbox1_y2, bbox1_x2 = bbox1
    bbox2_y1, bbox2_x1, bbox2_y2, bbox2_x2 = bbox2

    intersection_y1 = max(bbox1_y1, bbox2_y1)
    intersection_x1 = max(bbox1_x1, bbox2_x1)
    intersection_y2 = min(bbox1_y2, bbox2_y2)
    intersection_x2 = min(bbox1_x2, bbox2_x2)

    intersection_area = max(intersection_x2 - intersection_x1 + 1, 0) * max(
        intersection_y2 - intersection_y1 + 1, 0
    )

    bbox1_area = (bbox1_x2 - bbox1_x1 + 1) * (bbox1_y2 - bbox1_y1 + 1)
    bbox2_area = (bbox2_x2 - bbox2_x1 + 1) * (bbox2_y2 - bbox2_y1 + 1)

    union_area = bbox1_area + bbox2_area - intersection_area

    iou = intersection_area / union_area

    return iou


def nms_bboxes(
    bboxes: np.ndarray,
    bbox_scores: np.ndarray,
    iou_threshold: float = 0.2,
    max_boxes: int = 128,
) -> np.ndarray:
    """Selects a subset of bounding boxes by NMS to minimize overlaps.

    This function is a convenience wrapper around the `TensorFlow NMS implementation
    <https://www.tensorflow.org/api_docs/python/tf/image/non_max_suppression_with_scores>`_.

    Args:
        bboxes: An array of shape (n_bboxes, 4) with rows specifying bounding boxes in
            the format [y1, x1, y2, x2].
        bbox_scores: An array of shape (n_bboxes,) specifying the score associated with
            each bounding box. These will be used to prioritize suppression.
        iou_threshold: The minimum intersection over union between a pair of bounding
            boxes to consider them as overlapping.
        max_boxes: The maximum number of bounding boxes to output.
    
    Returns:
        merged_bboxes a numpy array of shape (n_merged_bboxes, 4) corresponding to a
        subset of the bounding boxes after suppressing overlaps.
    """

    selected_indices, selected_scores = tf.image.non_max_suppression_with_scores(
        bboxes, bbox_scores, max_output_size=max_boxes, iou_threshold=iou_threshold
    )

    return bboxes[selected_indices.numpy()]


def merge_bboxes(bboxes, bbox_scores, merge_box_length, merge_min_iou=0.1):

    candidate_bboxes = []
    candidate_bbox_scores = []
    for i in range(len(bboxes) - 1):

        bbox_i = bboxes[i]
        for j in range(i + 1, len(bboxes)):
            bbox_j = bboxes[j]

            iou = compute_iou(bbox_i, bbox_j)

            if iou > merge_min_iou:
                middle_centroid = np.array(
                    [(bbox_i[0] + bbox_j[2]) / 2, (bbox_i[1] + bbox_j[3]) / 2]
                )
                candidate_bboxes.append(
                    [
                        middle_centroid[0] - merge_box_length / 2,  # y1
                        middle_centroid[1] - merge_box_length / 2,  # x1
                        middle_centroid[0] + merge_box_length / 2,  # y2
                        middle_centroid[1] + merge_box_length / 2,  # x2
                    ]
                )
                candidate_bbox_scores.append((bbox_scores[i] + bbox_scores[j]))

    candidate_bboxes.extend(list(bboxes))
    candidate_bbox_scores.extend(list(bbox_scores))

    return np.stack(candidate_bboxes, axis=0), np.stack(candidate_bbox_scores, axis=0)


def generate_merged_bboxes(
    bboxes: np.ndarray,
    bbox_scores: np.ndarray,
    merged_box_length: int,
    merge_iou_threshold: float = 0.1,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generates new candidate region proposals by merging overlapping bounding boxes.

    This function will generate new bounding boxes by merging bounding boxes that meet
    the specified IOU threshold by placing a new centered bounding box at the midpoint
    between both boxes.

    Args:
        bboxes: A starting set of possibly overlapping bounding boxes.
        bbox_scores: The corresponding scores with each bounding box.
        merged_box_length: A scalar int specifying the width and height of merged
            bounding boxes. Set this to a larger size than the original bboxes such
            that the resulting merged bbox encompasses both of the original bounding
            boxes. A conservative value is twice the original bounding box length.
        merge_iou_threshold: Scalar float specifying the minimum IOU between each pair
            of bounding boxes in order to generate a new merged bounding box.

    Returns:
        A tuple of (merged_bboxes, merged_bbox_scores).

        merged_bboxes: A numpy array of shape (n_merged_bboxes, 4) specified in the 
            [y1, x1, y2, x2] format. This is a superset of the input bboxes and the
            new merged region proposals.
        merged_bbox_scores: A numpy array of shape (n_merged_bboxes,) with the
            corresponding scores. Merged bboxes will have a score that is the sum
            of the original bboxes.
    """

    # Check every pair of bounding boxes for mergers.
    merged_centroids = []
    merged_bbox_scores = []
    for (bbox_i, score_i), (bbox_j, score_j) in itertools.combinations(
        zip(bboxes, bbox_scores), 2
    ):

        # We'll generate a new merged bounding box if the pair overlaps sufficiently.
        if compute_iou(bbox_i, bbox_j) > merge_iou_threshold:

            # Compute midpoint and combined score.
            merged_centroids.append(
                [(bbox_i[0] + bbox_j[2]) / 2, (bbox_i[1] + bbox_j[3]) / 2]
            )
            merged_bbox_scores.append(score_i + score_j)

    merged_centroids = np.array(merged_centroids)
    merged_bbox_scores = np.array(merged_bbox_scores)

    if len(merged_centroids) > 0:
        # Create bounding boxes from the new centroids.
        merged_bboxes = make_centered_bboxes(
            merged_centroids, box_length=merged_box_length, center_offset=False
        )

    else:
        # No mergers detected.
        merged_bboxes = np.empty((0, 4), dtype="float32")

    # Combine with the original bboxes.
    merged_bboxes = np.concatenate((bboxes, merged_bboxes), axis=0)
    merged_bbox_scores = np.concatenate((bbox_scores, merged_bbox_scores), axis=0)

    return merged_bboxes, merged_bbox_scores


def normalize_bboxes(bboxes: np.ndarray, img_height: int, img_width: int) -> np.ndarray:
    """Normalizes bounding boxes from absolute to relative image coordinates.

    Args:
        bboxes: An array of shape (n_bboxes, 4) with rows specifying bounding boxes in
            the format [y1, x1, y2, x2] in absolute image coordinates (in pixels).
        img_height: Height of image in pixels.
        img_width: Width of image in pixels.

    Returns:
        Normalized bounding boxes where all coordinates are in the range [0, 1].
    """

    h = img_height - 1.0
    w = img_width - 1.0
    return bboxes / np.array([[h, w, h, w]])


def predict_centroids(
    imgs: tf.Tensor, centroid_model: InferenceModel, batch_size: int = 16
) -> Tuple[np.ndarray, np.ndarray]:
    """Predicts centroids given a stack of images and a centroid model.

    This function is a convenience wrapper for extracting centroids from a stack of
    images. It performs resizing/preprocessing, batched model inference and adjusts the
    resolution of the output coordinates.

    Args:
        imgs: A tensor of shape (samples, height, width, channels) to provide as input
            to the trained model.
        centroid_model: An InferenceModel containing the tf.keras.Model for predicting
            centroid heatmaps, together with metadata about the architecture and job.
        batch_size: Number of samples to process on the GPU at a time.

    Returns:
        A tuple of (centroid_peaks, centroid_peak_vals).

        centroid_peaks is a float32 tensor of shape (n_centroids, 4), where rows
        indicate subscripts to detected peaks in the form (sample, row, col, channel).
    """

    # Preprocess
    resized_imgs = utils.resize_imgs(
        imgs, centroid_model.input_scale, common_divisor=2 ** centroid_model.down_blocks
    )
    resized_imgs = tf.cast(resized_imgs, tf.float32) / 255.0

    # Model inference
    centroid_confmaps = utils.batched_call(
        centroid_model.keras_model, resized_imgs, batch_size=batch_size
    )

    # Peak finding
    centroids, centroid_vals = peak_finding.find_local_peaks(centroid_confmaps)
    centroids = centroids.numpy().astype("float32")
    centroid_vals = centroid_vals.numpy()
    centroids /= np.array(
        [[1, centroid_model.output_scale, centroid_model.output_scale, 1]]
    )

    return centroids, centroid_vals


def extract_region_proposals(
    imgs: tf.Tensor,
    centroids: np.ndarray,
    centroid_vals: np.ndarray,
    instance_box_length: int,
    merged_box_length: int,
    merge_iou_threshold: float = 0.1,
    nms_iou_threshold: float = 0.25,
) -> List[RegionProposalSet]:
    """Extracts a set of centered patches given detected centroids.

    This function will attempt to merge overlapping bounding boxes and group them
    accordingly, together with metadata about the region proposals.

    Args:
        imgs: A tensor of shape (samples, height, width, channels) to provide as input
            to the trained model.
        centroids: A float32 numpy array of shape (n_centroids, 4), where rows indicate
            subscripts to detected peaks in the form (sample, row, col, channel).
        centroid_vals: A float32 vector corresponding to the centroids that will be
            used as the scores for the generated centered bounding boxes for subsequent
            overlap merging via NMS.
        instance_box_length: Scalar int specifying the width and height of bounding
            boxes centered on individual instances (detected by centroids).
        merged_box_length: Scalar int specifing the width and height of the bounding
            boxes that will be attempted to be created when merging overlapping
            instances. This should be >= instance_box_length.
        merge_iou_threshold: Overlap threshold in order to generate candidate merged
            boxes at the midpoint between overlapping instances.
        nms_iou_threshold: Overlap threshold to use for suppressing bounding box
            overlaps via NMS. See nms_bboxes for more info.

    Returns:
        A list of RegionProposalSet instances, where each set consists of bounding box
        metadata as well as the patches extracted from the final bounding boxes after
        merging and filtering.
    """

    # Create initial region proposals from bounding boxes centered on the centroids.
    all_bboxes = make_centered_bboxes(centroids, instance_box_length)

    # Group region proposals by sample indices.
    sample_inds = centroids[:, 0].astype(int)
    sample_grouped_bboxes = utils.group_array(all_bboxes, sample_inds)
    sample_grouped_bbox_scores = utils.group_array(centroid_vals, sample_inds)

    # Merge bounding boxes that are closely overlapping.
    merged_bboxes = dict()
    for sample in sample_grouped_bboxes.keys():

        # Generate new candidates by merging overlapping bounding boxes.
        candidate_bboxes, candidate_bbox_scores = generate_merged_bboxes(
            sample_grouped_bboxes[sample],
            sample_grouped_bbox_scores[sample],
            merged_box_length=merged_box_length,
            merge_iou_threshold=merge_iou_threshold,
        )

        # Suppress overlaps including merged proposals.
        merged_bboxes[sample] = nms_bboxes(
            candidate_bboxes, candidate_bbox_scores, iou_threshold=nms_iou_threshold
        )

    # Group merged proposals by size.
    size_grouped_bboxes = defaultdict(list)
    size_grouped_sample_inds = defaultdict(list)
    for sample_ind, sample_bboxes in merged_bboxes.items():
        for bbox in sample_bboxes:

            # Compute (height, width) of bounding box.
            box_size = (int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1]))

            # Add to the size group.
            size_grouped_bboxes[box_size].append(bbox)
            size_grouped_sample_inds[box_size].append(sample_ind)

    # Generate proposal sets by size.
    region_proposal_sets = []
    for box_size in size_grouped_bboxes.keys():

        # Gather size grouped data.
        sample_inds = np.stack(size_grouped_sample_inds[box_size])
        bboxes = np.stack(size_grouped_bboxes[box_size])

        # Extract image patches for all regions in the set.
        patches = tf.image.crop_and_resize(
            imgs,
            boxes=normalize_bboxes(bboxes, imgs.shape[1], imgs.shape[2]),
            box_indices=sample_inds,
            crop_size=box_size,
        )

        # Save proposal set.
        region_proposal_sets.append(
            RegionProposalSet(box_size, sample_inds, bboxes, patches)
        )

    return region_proposal_sets
