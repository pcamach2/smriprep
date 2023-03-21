# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
#
# Copyright 2021 The NiPreps Developers <nipreps@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# We support and encourage derived works from this project, please read
# about our expectations at
#
#     https://www.nipreps.org/community/licensing/
#
"""
Surface preprocessing workflows.

**sMRIPrep** uses FreeSurfer to reconstruct surfaces from T1w/T2w
structural images or FastSurfer for a machine learning-accelerated
alternative using only T1w structural images.

"""
import typing as ty
from nipype.pipeline import engine as pe
from nipype.interfaces.base import Undefined
from nipype.interfaces import (
    fsl,
    io as nio,
    utility as niu,
    freesurfer as fs,
    workbench as wb,
)

from ..interfaces.freesurfer import ReconAll, MakeMidthickness
from ..interfaces import fastsurfer as fastsurf

from niworkflows.engine.workflows import LiterateWorkflow as Workflow
from niworkflows.interfaces.freesurfer import (
    FSDetectInputs,
    FSInjectBrainExtracted,
    PatchedLTAConvert as LTAConvert,
    PatchedRobustRegister as RobustRegister,
    RefineBrainMask,
)

from nipype import logging
# import os

LOGGER = logging.getLogger("nipype.workflow")


def init_fastsurf_recon_wf(*, omp_nthreads, hires, name="fastsurf_recon_wf"):
    r"""
    Reconstruct anatomical surfaces using FastSurfer VINN and ``recon_surf``,
    an alternative to FreeSurfer's ``recon-all``.

    First, FastSurfer VINN creates a segmentation using the T1w structural image.
    This is followed by FastSurfer ``recon_surf`` processing of the surface,
    along with surface registration to support cross-subject comparison
    using the ``--surfeg`` argument to FastSurfer.

    For example, a subject with only one session with a T1w image
    would be processed by the following command::

        $ /opt/FastSurfer/run_fastsurfer.sh \
        --t1 <bids-root>/sub-<subject_label>/anat/sub-<subject_label>_T1w.nii.gz \
        --sid sub-<subject_label> --sd <output dir>/fastsurfer --surfreg

    Similar to the Freesurfer workflow second phase, we then
    import an externally computed skull-stripping mask.
    This workflow refines the external brainmask using the internal mask
    implicit the FastSurfer's ``aseg.mgz`` segmentation,
    to reconcile ANTs' and FreeSurfer's brain masks.

    First, the ``aseg.mgz`` mask from FastSurfer is refined in two
    steps, using binary morphological operations:

      1. With a binary closing operation the sulci are included
         into the mask. This results in a smoother brain mask
         that does not exclude deep, wide sulci.

      2. Fill any holes (typically, there could be a hole next to
         the pineal gland and the corpora quadrigemina if the great
         cerebral brain is segmented out).

    Second, the brain mask is grown, including pixels that have a high likelihood
    to the GM tissue distribution:

      3. Dilate and substract the brain mask, defining the region to search for candidate
         pixels that likely belong to cortical GM.

      4. Pixels found in the search region that are labeled as GM by ANTs
         (during ``antsBrainExtraction.sh``) are directly added to the new mask.

      5. Otherwise, estimate GM tissue parameters locally in  patches of ``ww`` size,
         and test the likelihood of the pixel to belong in the GM distribution.

    This procedure is inspired on mindboggle's solution to the problem:
    https://github.com/nipy/mindboggle/blob/7f91faaa7664d820fe12ccc52ebaf21d679795e2/mindboggle/guts/segment.py#L1660

    Memory annotations for FastSurfer are based off `their documentation
    <https://github.com/Deep-MI/FastSurfer/tree/9a424a83d2a24d36fe4519c2105c608df71bb2a0#system-requirements>`_.
    They recommend 8GB CPU RAM and 8GB NVIDIA GPU RAM to run a single batch.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from smriprep.workflows.surfaces import init_fastsurf_recon_wf
            wf = init_fastsurf_recon_wf(omp_nthreads=1)

    Required arguments
    ==================
    sd
        Output directory
    sid
        Subject ID for directory inside ``sd`` to be created
    t1
        T1 full head input (not bias corrected, global path).
        The 'network was trained with conformed images
        (UCHAR, 256x256x256, 1 mm voxels and standard slice orientation).
        These specifications are checked in the ``eval.py`` script and the image
        is automatically conformed if it does not comply.

    Optional arguments
    ==================

    Network specific arguments
    --------------------------
    seg
        Global path with filename of segmentation
        (where and under which name to store it).
        Default location
            ``$SUBJECTS_DIR/$sid/mri/aparc.DKTatlas+aseg.deep.mgz``
    weights_sag
        Pretrained weights of sagittal network.
        Default
            ``../checkpoints/Sagittal_Weights_FastSurferVINN/ckpts/Epoch_30_training_state.pkl``
    weights_ax
        Pretrained weights of axial network.
        Default
            ``../checkpoints/Axial_Weights_FastSurferVINN/ckpts/Epoch_30_training_state.pkl``
    weights_cor
        Pretrained weights of coronal network.
        Default
            ``../checkpoints/Coronal_Weights_FastSurferVINN/ckpts/Epoch_30_training_state.pkl``
    seg_log
        Name and location for the log-file for the segmentation (FastSurferVINN).
        Default '$SUBJECTS_DIR/$sid/scripts/deep-seg.log'
    clean_seg
        Flag to clean up FastSurferVINN segmentation
    run_viewagg_on
        Define where the view aggregation should be run on.
        By default, the program checks if you have enough memory to run
        the view aggregation on the gpu. The total memory is considered for this decision.
        If this fails, or you actively overwrote the check with setting
        ``run_viewagg_on cpu``, view agg is run on the cpu.
        Equivalently, if you define ``--run_viewagg_on gpu``, view agg will be run on the gpu
        (no memory check will be done).
    no_cuda
        Flag to disable CUDA usage in FastSurferVINN (no GPU usage, inference on CPU)
    batch
        Batch size for inference. Default 16. Lower this to reduce memory requirement
    order
        Order of interpolation for mri_convert T1 before segmentation
        ``(0=nearest, 1=linear(default), 2=quadratic, 3=cubic)``

    Surface pipeline arguments
    --------------------------
    fstess
        Use ``mri_tesselate`` instead of marching cube (default) for surface creation
    fsqsphere
        Use FreeSurfer default instead of
        novel spectral spherical projection for qsphere
    fsaparc
        Use FS aparc segmentations in addition to DL prediction
        (slower in this case and usually the mapped ones from the DL prediction are fine)
    surfreg
        Create Surface-Atlas ``sphere.reg`` registration with FreeSurfer
        (for cross-subject correspondence or other mappings)
    parallel
        Run both hemispheres in parallel
    threads
        Set openMP and ITK threads

    Other
    ----
    py
        which python version to use.
        Default ``python3.6``
    seg_only
        only run FastSurferVINN
        (generate segmentation, do not run the surface pipeline)
    surf_only
        only run the surface pipeline ``recon_surf``.
        The segmentation created by FastSurferVINN must already exist in this case.


    """
    workflow = Workflow(name=name)
    workflow.__desc__ = """\
Brain surfaces were reconstructed using `recon-surf` [FastSurfer {fastsurfer_version},
@fastsurfer], and the brain mask estimated
previously was refined with a custom variation of the method to reconcile
ANTs-derived and FastSurfer-derived segmentations of the cortical
gray-matter of Mindboggle [RRID:SCR_002438, @mindboggle].
""".format(
        fastsurfer_version="2.0.4"
    )

    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "sd",
                "sid",
                "t1w",
                "skullstripped_t1",
                "corrected_t1",
                "ants_segs",
            ]
        ),
        name="inputnode",
    )
    outputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "subjects_dir",
                "subject_id",
                "t1w2fsnative_xfm",
                "fsnative2t1w_xfm",
                "surfaces",
                "out_brainmask",
                "out_aseg",
                "out_aparc",
                "morphometrics",
            ]
        ),
        name="outputnode",
    )

    # outputnode = pe.Node(niu.IdentityInterface(["surfaces", "morphometrics"]), name="outputnode")

    # null for now, placeholder for FastSurfer VINN hires support
    if hires:
        if LOGGER:
            LOGGER.warn(f'High-resolution {hires} specified, not currently supported, ignoring.')

    fsnative2t1w_xfm = pe.Node(
        RobustRegister(auto_sens=True, est_int_scale=True), name="fsnative2t1w_xfm")
    t1w2fsnative_xfm = pe.Node(
        LTAConvert(out_lta=True, invert=True), name="t1w2fsnative_xfm")

    # inputnode.in_file = inputnode.t1w
    gifti_surface_wf = init_gifti_surface_wf(fastsurfer=True)
    aseg_to_native_wf = init_segs_to_native_wf(fastsurfer=True)
    aparc_to_native_wf = init_segs_to_native_wf(fastsurfer=True, segmentation="aparc_aseg")
    refine = pe.Node(RefineBrainMask(), name="refine")
    # fs_license_file variable not used, uncomment if this changes!
    # fs_license_file = "/opt/freesurfer/license.txt"
    # if os.path.exists("/tmp/freesurfer/license.txt"):
    #     fs_license_file = "/tmp/freesurfer/license.txt"
    # elif os.environ['FS_LICENSE']:
    #     fs_license_file = os.environ['FS_LICENSE']

    # recon_conf = pe.Node(
    #     fastsurf.FastSurferSource(), name="recon_conf")

    fastsurf_recon = pe.Node(
        fastsurf.FastSurfer(),
        name="fastsurf_recon",
        n_procs=omp_nthreads,
        mem_gb=12,
    )
    fastsurf_recon.interface._can_resume = False
    fastsurf_recon.interface._always_run = True

    # fov_check = pe.Node(niu.Function(function=_check_cw256), name="fov_check")

    skull_strip_extern = pe.Node(FSInjectBrainExtracted(), name="skull_strip_extern")

    fsnative2t1w_xfm = pe.Node(
        RobustRegister(auto_sens=True, est_int_scale=True), name="fsnative2t1w_xfm"
    )
    t1w2fsnative_xfm = pe.Node(
        LTAConvert(out_lta=True, invert=True), name="t1w2fsnative_xfm"
    )

    # fmt:off
    workflow.connect([
        # Configuration
        # (inputnode, recon_conf, [('t1w', 't1'),
        #                          ('sd', 'sd'),
        #                          ('sid', 'sid')]),
        (inputnode, fastsurf_recon, [('sd', 'subjects_dir'),
                                     ('sid', 'subject_id'),
                                     ('t1w', 'T1_files')]),
        (inputnode, skull_strip_extern, [('sd', 'subjects_dir'),
                                         ('sid', 'subject_id')]),
        (inputnode, gifti_surface_wf, [
            ('sd', 'inputnode.subjects_dir'),
            ('sid', 'inputnode.subject_id')]),
        (inputnode, skull_strip_extern, [('skullstripped_t1', 'in_brain')]),
        # Construct transform from FreeSurfer conformed image to sMRIPrep
        # reoriented image
        (inputnode, fsnative2t1w_xfm, [('t1w', 'target_file')]),
        (fastsurf_recon, fsnative2t1w_xfm, [('T1', 'source_file')]),
        (fsnative2t1w_xfm, gifti_surface_wf, [
            ('out_reg_file', 'inputnode.fsnative2t1w_xfm')]),
        (fsnative2t1w_xfm, t1w2fsnative_xfm, [('out_reg_file', 'in_lta')]),
        # Refine ANTs mask, deriving new mask from FS' aseg
        (inputnode, refine, [('corrected_t1', 'in_anat'),
                             ('ants_segs', 'in_ants')]),
        (inputnode, aseg_to_native_wf, [('corrected_t1', 'inputnode.in_file')]),
        (fsnative2t1w_xfm, aseg_to_native_wf, [
            ('out_reg_file', 'inputnode.fsnative2t1w_xfm')]),
        (inputnode, aparc_to_native_wf, [('corrected_t1', 'inputnode.in_file')]),
        (fsnative2t1w_xfm, aparc_to_native_wf, [
            ('out_reg_file', 'inputnode.fsnative2t1w_xfm')]),
        (aseg_to_native_wf, refine, [('outputnode.out_file', 'in_aseg')]),

        # Output
        (inputnode, outputnode, [('sd', 'subjects_dir'),
                                 ('sid', 'subject_id')]),
        (gifti_surface_wf, outputnode, [('outputnode.surfaces', 'surfaces'),
                                        ('outputnode.morphometrics', 'morphometrics')]),
        (t1w2fsnative_xfm, outputnode, [('out_lta', 't1w2fsnative_xfm')]),
        (fsnative2t1w_xfm, outputnode, [('out_reg_file', 'fsnative2t1w_xfm')]),
        (refine, outputnode, [('out_file', 'out_brainmask')]),
        (aseg_to_native_wf, outputnode, [('outputnode.out_file', 'out_aseg')]),
        (aparc_to_native_wf, outputnode, [('outputnode.out_file', 'out_aparc')]),
    ])
    # fmt:on

    return workflow

from ..interfaces.workbench import CreateSignedDistanceVolume


def init_surface_recon_wf(*, omp_nthreads, hires, name="surface_recon_wf"):
    r"""
    Reconstruct anatomical surfaces using FreeSurfer's ``recon-all``.

    Reconstruction is performed in three phases.
    The first phase initializes the subject with T1w and T2w (if available)
    structural images and performs basic reconstruction (``autorecon1``) with the
    exception of skull-stripping.
    For example, a subject with only one session with T1w and T2w images
    would be processed by the following command::

        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -i <bids-root>/sub-<subject_label>/anat/sub-<subject_label>_T1w.nii.gz \
            -T2 <bids-root>/sub-<subject_label>/anat/sub-<subject_label>_T2w.nii.gz \
            -autorecon1 \
            -noskullstrip -noT2pial -noFLAIRpial

    The second phase imports an externally computed skull-stripping mask.
    This workflow refines the external brainmask using the internal mask
    implicit the the FreeSurfer's ``aseg.mgz`` segmentation,
    to reconcile ANTs' and FreeSurfer's brain masks.

    First, the ``aseg.mgz`` mask from FreeSurfer is refined in two
    steps, using binary morphological operations:

      1. With a binary closing operation the sulci are included
         into the mask. This results in a smoother brain mask
         that does not exclude deep, wide sulci.

      2. Fill any holes (typically, there could be a hole next to
         the pineal gland and the corpora quadrigemina if the great
         cerebral brain is segmented out).

    Second, the brain mask is grown, including pixels that have a high likelihood
    to the GM tissue distribution:

      3. Dilate and substract the brain mask, defining the region to search for candidate
         pixels that likely belong to cortical GM.

      4. Pixels found in the search region that are labeled as GM by ANTs
         (during ``antsBrainExtraction.sh``) are directly added to the new mask.

      5. Otherwise, estimate GM tissue parameters locally in  patches of ``ww`` size,
         and test the likelihood of the pixel to belong in the GM distribution.

    This procedure is inspired on mindboggle's solution to the problem:
    https://github.com/nipy/mindboggle/blob/7f91faaa7664d820fe12ccc52ebaf21d679795e2/mindboggle/guts/segment.py#L1660

    The final phase resumes reconstruction, using the T2w image to assist
    in finding the pial surface, if available.
    See :py:func:`~smriprep.workflows.surfaces.init_autorecon_resume_wf` for details.

    Memory annotations for FreeSurfer are based off `their documentation
    <https://surfer.nmr.mgh.harvard.edu/fswiki/SystemRequirements>`_.
    They specify an allocation of 4GB per subject. Here we define 5GB
    to have a certain margin.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from smriprep.workflows.surfaces import init_surface_recon_wf
            wf = init_surface_recon_wf(omp_nthreads=1, hires=True)

    Parameters
    ----------
    omp_nthreads : int
        Maximum number of threads an individual process may use
    hires : bool
        Enable sub-millimeter preprocessing in FreeSurfer

    Inputs
    ------
    t1w
        List of T1-weighted structural images
    t2w
        List of T2-weighted structural images (only first used)
    flair
        List of FLAIR images
    skullstripped_t1
        Skull-stripped T1-weighted image (or mask of image)
    ants_segs
        Brain tissue segmentation from ANTS ``antsBrainExtraction.sh``
    corrected_t1
        INU-corrected, merged T1-weighted image
    subjects_dir
        FreeSurfer SUBJECTS_DIR
    subject_id
        FreeSurfer subject ID

    Outputs
    -------
    subjects_dir
        FreeSurfer SUBJECTS_DIR
    subject_id
        FreeSurfer subject ID
    t1w2fsnative_xfm
        LTA-style affine matrix translating from T1w to FreeSurfer-conformed subject space
    fsnative2t1w_xfm
        LTA-style affine matrix translating from FreeSurfer-conformed subject space to T1w
    surfaces
        GIFTI surfaces for gray/white matter boundary, pial surface,
        midthickness (or graymid) surface, and inflated surfaces
    out_brainmask
        Refined brainmask, derived from FreeSurfer's ``aseg`` volume
    out_aseg
        FreeSurfer's aseg segmentation, in native T1w space
    out_aparc
        FreeSurfer's aparc+aseg segmentation, in native T1w space
    morphometrics
        GIFTIs of cortical thickness, curvature, and sulcal depth

    See also
    --------
    * :py:func:`~smriprep.workflows.surfaces.init_autorecon_resume_wf`
    * :py:func:`~smriprep.workflows.surfaces.init_gifti_surface_wf`

    """
    workflow = Workflow(name=name)
    workflow.__desc__ = """\
Brain surfaces were reconstructed using `recon-all` [FreeSurfer {fs_ver},
RRID:SCR_001847, @fs_reconall], and the brain mask estimated
previously was refined with a custom variation of the method to reconcile
ANTs-derived and FreeSurfer-derived segmentations of the cortical
gray-matter of Mindboggle [RRID:SCR_002438, @mindboggle].
""".format(
        fs_ver=fs.Info().looseversion() or "<ver>"
    )

    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "t1w",
                "t2w",
                "flair",
                "skullstripped_t1",
                "corrected_t1",
                "ants_segs",
                "subjects_dir",
                "subject_id",
            ]
        ),
        name="inputnode",
    )
    outputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "subjects_dir",
                "subject_id",
                "t1w2fsnative_xfm",
                "fsnative2t1w_xfm",
                "surfaces",
                "out_brainmask",
                "out_aseg",
                "out_aparc",
                "morphometrics",
            ]
        ),
        name="outputnode",
    )

    recon_config = pe.Node(FSDetectInputs(hires_enabled=hires), name="recon_config")

    fov_check = pe.Node(niu.Function(function=_check_cw256), name="fov_check")
    fov_check.inputs.default_flags = ['-noskullstrip', '-noT2pial', '-noFLAIRpial']

    autorecon1 = pe.Node(
        ReconAll(directive="autorecon1", openmp=omp_nthreads),
        name="autorecon1",
        n_procs=omp_nthreads,
        mem_gb=5,
    )
    autorecon1.interface._can_resume = False
    autorecon1.interface._always_run = True

    skull_strip_extern = pe.Node(FSInjectBrainExtracted(), name="skull_strip_extern")

    fsnative2t1w_xfm = pe.Node(
        RobustRegister(auto_sens=True, est_int_scale=True), name="fsnative2t1w_xfm"
    )
    t1w2fsnative_xfm = pe.Node(
        LTAConvert(out_lta=True, invert=True), name="t1w2fsnative_xfm"
    )

    autorecon_resume_wf = init_autorecon_resume_wf(omp_nthreads=omp_nthreads)
    gifti_surface_wf = init_gifti_surface_wf(fastsurfer=False)
    aseg_to_native_wf = init_segs_to_native_wf(fastsurfer=False)
    aparc_to_native_wf = init_segs_to_native_wf(fastsurfer=False, segmentation="aparc_aseg")
    refine = pe.Node(RefineBrainMask(), name="refine")

    # fmt:off
    workflow.connect([
        # Configuration
        (inputnode, recon_config, [('t1w', 't1w_list'),
                                   ('t2w', 't2w_list'),
                                   ('flair', 'flair_list')]),
        # Passing subjects_dir / subject_id enforces serial order
        (inputnode, autorecon1, [('subjects_dir', 'subjects_dir'),
                                 ('subject_id', 'subject_id')]),
        (autorecon1, skull_strip_extern, [('subjects_dir', 'subjects_dir'),
                                          ('subject_id', 'subject_id')]),
        (skull_strip_extern, autorecon_resume_wf, [('subjects_dir', 'inputnode.subjects_dir'),
                                                   ('subject_id', 'inputnode.subject_id')]),
        (autorecon_resume_wf, gifti_surface_wf, [
            ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
            ('outputnode.subject_id', 'inputnode.subject_id')]),
        # Reconstruction phases
        (inputnode, autorecon1, [('t1w', 'T1_files')]),
        (inputnode, fov_check, [('t1w', 'in_files')]),
        (fov_check, autorecon1, [('out', 'flags')]),
        (recon_config, autorecon1, [('t2w', 'T2_file'),
                                    ('flair', 'FLAIR_file'),
                                    ('hires', 'hires'),
                                    # First run only (recon-all saves expert options)
                                    ('mris_inflate', 'mris_inflate')]),
        (inputnode, skull_strip_extern, [('skullstripped_t1', 'in_brain')]),
        (recon_config, autorecon_resume_wf, [('use_t2w', 'inputnode.use_T2'),
                                             ('use_flair', 'inputnode.use_FLAIR')]),
        # Construct transform from FreeSurfer conformed image to sMRIPrep
        # reoriented image
        (inputnode, fsnative2t1w_xfm, [('t1w', 'target_file')]),
        (autorecon1, fsnative2t1w_xfm, [('T1', 'source_file')]),
        (fsnative2t1w_xfm, t1w2fsnative_xfm, [('out_reg_file', 'in_lta')]),
        # Refine ANTs mask, deriving new mask from FS' aseg
        (inputnode, refine, [('corrected_t1', 'in_anat'),
                             ('ants_segs', 'in_ants')]),
        (inputnode, aseg_to_native_wf, [('corrected_t1', 'inputnode.in_file')]),
        (autorecon_resume_wf, aseg_to_native_wf, [
            ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
            ('outputnode.subject_id', 'inputnode.subject_id')]),
        (fsnative2t1w_xfm, aseg_to_native_wf, [('out_reg_file', 'inputnode.fsnative2t1w_xfm')]),
        (inputnode, aparc_to_native_wf, [('corrected_t1', 'inputnode.in_file')]),
        (autorecon_resume_wf, aparc_to_native_wf, [
            ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
            ('outputnode.subject_id', 'inputnode.subject_id')]),
        (fsnative2t1w_xfm, aparc_to_native_wf, [('out_reg_file', 'inputnode.fsnative2t1w_xfm')]),
        (aseg_to_native_wf, refine, [('outputnode.out_file', 'in_aseg')]),

        # Output
        (autorecon_resume_wf, outputnode, [('outputnode.subjects_dir', 'subjects_dir'),
                                           ('outputnode.subject_id', 'subject_id')]),
        (gifti_surface_wf, outputnode, [('outputnode.surfaces', 'surfaces'),
                                        ('outputnode.morphometrics', 'morphometrics')]),
        (t1w2fsnative_xfm, outputnode, [('out_lta', 't1w2fsnative_xfm')]),
        (fsnative2t1w_xfm, outputnode, [('out_reg_file', 'fsnative2t1w_xfm')]),
        (refine, outputnode, [('out_file', 'out_brainmask')]),
        (aseg_to_native_wf, outputnode, [('outputnode.out_file', 'out_aseg')]),
        (aparc_to_native_wf, outputnode, [('outputnode.out_file', 'out_aparc')]),
    ])
    # fmt:on

    return workflow


def init_autorecon_resume_wf(*, omp_nthreads, name="autorecon_resume_wf"):
    r"""
    Resume recon-all execution, assuming the `-autorecon1` stage has been completed.

    In order to utilize resources efficiently, this is broken down into seven
    sub-stages; after the first stage, the second and third stages may be run
    simultaneously, and the fifth and sixth stages may be run simultaneously,
    if resources permit; the fourth stage must be run prior to the fifth and
    sixth, and the seventh must be run after::

        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -autorecon2-volonly
        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -autorecon-hemi lh -T2pial \
            -noparcstats -noparcstats2 -noparcstats3 -nohyporelabel -nobalabels
        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -autorecon-hemi rh -T2pial \
            -noparcstats -noparcstats2 -noparcstats3 -nohyporelabel -nobalabels
        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -cortribbon
        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -autorecon-hemi lh -nohyporelabel
        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -autorecon-hemi rh -nohyporelabel
        $ recon-all -sd <output dir>/freesurfer -subjid sub-<subject_label> \
            -autorecon3

    The parcellation statistics steps are excluded from the second and third
    stages, because they require calculation of the cortical ribbon volume
    (the fourth stage).
    Hypointensity relabeling is excluded from hemisphere-specific steps to avoid
    race conditions, as it is a volumetric operation.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from smriprep.workflows.surfaces import init_autorecon_resume_wf
            wf = init_autorecon_resume_wf(omp_nthreads=1)

    Inputs
    ------
    subjects_dir
        FreeSurfer SUBJECTS_DIR
    subject_id
        FreeSurfer subject ID
    use_T2
        Refine pial surface using T2w image
    use_FLAIR
        Refine pial surface using FLAIR image

    Outputs
    -------
    subjects_dir
        FreeSurfer SUBJECTS_DIR
    subject_id
        FreeSurfer subject ID

    """
    workflow = Workflow(name=name)

    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=["subjects_dir", "subject_id", "use_T2", "use_FLAIR"]
        ),
        name="inputnode",
    )

    outputnode = pe.Node(
        niu.IdentityInterface(fields=["subjects_dir", "subject_id"]), name="outputnode"
    )

    # FreeSurfer 7.3 removed gcareg from autorecon2-volonly
    # Adding it directly in would force it to run every time
    gcareg = pe.Node(
        ReconAll(directive=Undefined, steps=["gcareg"], openmp=omp_nthreads),
        n_procs=omp_nthreads,
        mem_gb=5,
        name="gcareg",
    )
    gcareg.interface._always_run = True

    autorecon2_vol = pe.Node(
        ReconAll(directive="autorecon2-volonly", openmp=omp_nthreads),
        n_procs=omp_nthreads,
        mem_gb=5,
        name="autorecon2_vol",
    )
    autorecon2_vol.interface._always_run = True

    autorecon_surfs = pe.MapNode(
        ReconAll(
            directive="autorecon-hemi",
            flags=[
                "-noparcstats",
                "-noparcstats2",
                "-noparcstats3",
                "-nohyporelabel",
                "-nobalabels",
            ],
            openmp=omp_nthreads,
        ),
        iterfield="hemi",
        n_procs=omp_nthreads,
        mem_gb=5,
        name="autorecon_surfs",
    )
    autorecon_surfs.inputs.hemi = ["lh", "rh"]
    autorecon_surfs.interface._always_run = True

    # -cortribbon is a prerequisite for -parcstats, -parcstats2, -parcstats3
    # Claiming two threads because pial refinement can be split by hemisphere
    # if -T2pial or -FLAIRpial is enabled.
    # Parallelizing by hemisphere saves ~30 minutes over simply enabling
    # OpenMP on an 8 core machine.
    cortribbon = pe.Node(
        ReconAll(directive=Undefined, steps=["cortribbon"], parallel=True),
        n_procs=2,
        name="cortribbon",
    )
    cortribbon.interface._always_run = True

    # -parcstats* can be run per-hemisphere
    # -hyporelabel is volumetric, even though it's part of -autorecon-hemi
    parcstats = pe.MapNode(
        ReconAll(
            directive="autorecon-hemi", flags=["-nohyporelabel"], openmp=omp_nthreads
        ),
        iterfield="hemi",
        n_procs=omp_nthreads,
        mem_gb=5,
        name="parcstats",
    )
    parcstats.inputs.hemi = ["lh", "rh"]
    parcstats.interface._always_run = True

    # Runs: -hyporelabel -aparc2aseg -apas2aseg -segstats -wmparc
    # All volumetric, so don't
    autorecon3 = pe.Node(
        ReconAll(directive="autorecon3", openmp=omp_nthreads),
        n_procs=omp_nthreads,
        mem_gb=5,
        name="autorecon3",
    )
    autorecon3.interface._always_run = True

    def _dedup(in_list):
        vals = set(in_list)
        if len(vals) > 1:
            raise ValueError(
                "Non-identical values can't be deduplicated:\n{!r}".format(in_list)
            )
        return vals.pop()

    # fmt:off
    workflow.connect([
        (inputnode, cortribbon, [('use_T2', 'use_T2'),
                                 ('use_FLAIR', 'use_FLAIR')]),
        (inputnode, gcareg, [('subjects_dir', 'subjects_dir'),
                             ('subject_id', 'subject_id')]),
        (gcareg, autorecon2_vol, [('subjects_dir', 'subjects_dir'),
                                  ('subject_id', 'subject_id')]),
        (autorecon2_vol, autorecon_surfs, [('subjects_dir', 'subjects_dir'),
                                           ('subject_id', 'subject_id')]),
        (autorecon_surfs, cortribbon, [(('subjects_dir', _dedup), 'subjects_dir'),
                                       (('subject_id', _dedup), 'subject_id')]),
        (cortribbon, parcstats, [('subjects_dir', 'subjects_dir'),
                                 ('subject_id', 'subject_id')]),
        (parcstats, autorecon3, [(('subjects_dir', _dedup), 'subjects_dir'),
                                 (('subject_id', _dedup), 'subject_id')]),
        (autorecon3, outputnode, [('subjects_dir', 'subjects_dir'),
                                  ('subject_id', 'subject_id')]),
    ])
    # fmt:on

    return workflow


def init_gifti_surface_wf(*, fastsurfer, name="gifti_surface_wf"):
    r"""
    Prepare GIFTI surfaces from a FreeSurfer subjects directory.

    If midthickness (or graymid) surfaces do not exist, they are generated and
    saved to the subject directory as ``lh/rh.midthickness``.
    These, along with the gray/white matter boundary (``lh/rh.smoothwm``), pial
    sufaces (``lh/rh.pial``) and inflated surfaces (``lh/rh.inflated``) are
    converted to GIFTI files.
    Additionally, the vertex coordinates are :py:class:`recentered
    <smriprep.interfaces.NormalizeSurf>` to align with native T1w space.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from smriprep.workflows.surfaces import init_gifti_surface_wf
            wf = init_gifti_surface_wf()

    Inputs
    ------
    subjects_dir
        FreeSurfer SUBJECTS_DIR
    subject_id
        FreeSurfer subject ID
    fsnative2t1w_xfm
        LTA formatted affine transform file (inverse)
    fastsurfer
        Boolean to indicate FastSurfer surface processing

    Outputs
    -------
    surfaces
        GIFTI surfaces for gray/white matter boundary, pial surface,
        midthickness (or graymid) surface, and inflated surfaces
    morphometrics
        GIFTIs of cortical thickness, curvature, and sulcal depth

    """
    from ..interfaces.freesurfer import MRIsConvertData
    from ..interfaces.surf import NormalizeSurf

    workflow = Workflow(name=name)

    inputnode = pe.Node(
        niu.IdentityInterface(["subjects_dir", "subject_id", "fsnative2t1w_xfm"]),
        name="inputnode",
    )
    outputnode = pe.Node(niu.IdentityInterface(["surfaces", "morphometrics"]), name="outputnode")

    get_surfaces = pe.Node(nio.FreeSurferSource(), name="get_surfaces")

    midthickness = pe.MapNode(
        MakeMidthickness(thickness=True, distance=0.5, out_name="midthickness"),
        iterfield="in_file",
        name="midthickness",
    )

    save_midthickness = pe.Node(
        nio.DataSink(parameterization=False), name="save_midthickness"
    )

    surface_list = pe.Node(
        niu.Merge(4, ravel_inputs=True),
        name="surface_list",
        run_without_submitting=True,
    )
    fs2gii = pe.MapNode(
        fs.MRIsConvert(out_datatype="gii", to_scanner=True), iterfield="in_file", name="fs2gii",
    )
    fix_surfs = pe.MapNode(NormalizeSurf(), iterfield="in_file", name="fix_surfs")
    surfmorph_list = pe.Node(
        niu.Merge(3, ravel_inputs=True),
        name="surfmorph_list",
        run_without_submitting=True,
    )
    morphs2gii = pe.MapNode(
        MRIsConvertData(out_datatype="gii"),
        iterfield="scalarcurv_file", name="morphs2gii",
    )

    # fmt:off
    workflow.connect([
        (inputnode, get_surfaces, [('subjects_dir', 'subjects_dir'),
                                   ('subject_id', 'subject_id')]),
        (inputnode, save_midthickness, [('subjects_dir', 'base_directory'),
                                        ('subject_id', 'container')]),
        # Generate midthickness surfaces and save to FreeSurfer derivatives
        (get_surfaces, midthickness, [('smoothwm', 'in_file'),
                                      ('graymid', 'graymid')]),
        (midthickness, save_midthickness, [('out_file', 'surf.@graymid')]),
        # Produce valid GIFTI surface files (dense mesh)
        (get_surfaces, surface_list, [('smoothwm', 'in1'),
                                      ('pial', 'in2'),
                                      ('inflated', 'in3')]),
        (save_midthickness, surface_list, [('out_file', 'in4')]),
        (surface_list, fs2gii, [('out', 'in_file')]),
        (fs2gii, fix_surfs, [('converted', 'in_file')]),
        (inputnode, fix_surfs, [('fsnative2t1w_xfm', 'transform_file')]),
        (fix_surfs, outputnode, [('out_file', 'surfaces')]),
        (get_surfaces, surfmorph_list, [('thickness', 'in1'),
                                        ('sulc', 'in2'),
                                        ('curv', 'in3')]),
        (surfmorph_list, morphs2gii, [('out', 'scalarcurv_file')]),
        (morphs2gii, outputnode, [('converted', 'morphometrics')]),
    ])
    # fmt:on
    return workflow


def init_segs_to_native_wf(*, fastsurfer, name="segs_to_native", segmentation="aseg"):
    """
    Get a segmentation from FreeSurfer conformed space into native T1w space.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from smriprep.workflows.surfaces import init_segs_to_native_wf
            wf = init_segs_to_native_wf()

    Parameters
    ----------
    segmentation
        The name of a segmentation ('aseg' or 'aparc_aseg' or 'wmparc')

    Inputs
    ------
    in_file
        Anatomical, merged T1w image after INU correction
    subjects_dir
        FreeSurfer SUBJECTS_DIR
    subject_id
        FreeSurfer subject ID
    fsnative2t1w_xfm
        LTA-style affine matrix translating from FreeSurfer-conformed subject space to T1w
    fastsurfer
        Boolean to indicate FastSurfer processing

    Outputs
    -------
    out_file
        The selected segmentation, after resampling in native space

    """
    workflow = Workflow(name="%s_%s" % (name, segmentation))
    inputnode = pe.Node(
        niu.IdentityInterface(
            ["in_file", "subjects_dir", "subject_id", "fsnative2t1w_xfm"]
        ),
        name="inputnode",
    )
    outputnode = pe.Node(niu.IdentityInterface(["out_file"]), name="outputnode")
    # Extract the aseg and aparc+aseg outputs
    fssource = pe.Node(nio.FreeSurferSource(), name="fs_datasource")

    # Resample from T1.mgz to T1w.nii.gz, applying any offset in fsnative2t1w_xfm,
    # and convert to NIfTI while we're at it
    resample = pe.Node(
        fs.ApplyVolTransform(transformed_file="seg.nii.gz", interp="nearest"),
        name="resample",
    )

    if segmentation.startswith("aparc"):
        if segmentation == "aparc_aseg":

            def _sel(x):
                return [parc for parc in x if "aparc+" in parc][0]  # noqa

        elif segmentation == "aparc_a2009s":

            def _sel(x):
                return [parc for parc in x if "a2009s+" in parc][0]  # noqa

        elif segmentation == "aparc_dkt":

            def _sel(x):
                return [parc for parc in x if "DKTatlas+" in parc][0]  # noqa

        segmentation = (segmentation, _sel)

    # fmt:off
    workflow.connect([
        (inputnode, fssource, [
            ('subjects_dir', 'subjects_dir'),
            ('subject_id', 'subject_id')]),
        (inputnode, resample, [('in_file', 'target_file'),
                               ('fsnative2t1w_xfm', 'lta_file')]),
        (fssource, resample, [(segmentation, 'source_file')]),
        (resample, outputnode, [('transformed_file', 'out_file')]),
    ])
    # fmt:on
    return workflow


def init_anat_ribbon_wf(name="anat_ribbon_wf"):
    DEFAULT_MEMORY_MIN_GB = 0.01
    workflow = pe.Workflow(name=name)

    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "surfaces",
                "t1w_mask",
            ]
        ),
        name="inputnode",
    )
    outputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "anat_ribbon",
            ]
        ),
        name="outputnode",
    )

    # 0, 1 = wm; 2, 3 = pial; 6, 7 = mid
    # note that order of lh / rh within each surf type is not guaranteed due to use
    # of unsorted glob by FreeSurferSource prior, but we can do a sort
    # to ensure consistent ordering
    select_wm = pe.Node(
        niu.Select(index=[0, 1]),
        name="select_wm",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
        run_without_submitting=True,
    )

    select_pial = pe.Node(
        niu.Select(index=[2, 3]),
        name="select_pial",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
        run_without_submitting=True,
    )

    select_midthick = pe.Node(
        niu.Select(index=[6, 7]),
        name="select_midthick",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
        run_without_submitting=True,
    )

    create_wm_distvol = pe.MapNode(
        CreateSignedDistanceVolume(),
        iterfield=["surf_file"],
        name="create_wm_distvol",
    )

    create_pial_distvol = pe.MapNode(
        CreateSignedDistanceVolume(),
        iterfield=["surf_file"],
        name="create_pial_distvol",
    )

    thresh_wm_distvol = pe.MapNode(
        fsl.maths.MathsCommand(args="-thr 0 -bin -mul 255"),
        iterfield=["in_file"],
        name="thresh_wm_distvol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    uthresh_pial_distvol = pe.MapNode(
        fsl.maths.MathsCommand(args="-uthr 0 -abs -bin -mul 255"),
        iterfield=["in_file"],
        name="uthresh_pial_distvol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    bin_wm_distvol = pe.MapNode(
        fsl.maths.UnaryMaths(operation="bin"),
        iterfield=["in_file"],
        name="bin_wm_distvol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    bin_pial_distvol = pe.MapNode(
        fsl.maths.UnaryMaths(operation="bin"),
        iterfield=["in_file"],
        name="bin_pial_distvol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    split_wm_distvol = pe.Node(
        niu.Split(splits=[1, 1]),
        name="split_wm_distvol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
        run_without_submitting=True,
    )

    merge_wm_distvol_no_flatten = pe.Node(
        niu.Merge(2),
        no_flatten=True,
        name="merge_wm_distvol_no_flatten",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
        run_without_submitting=True,
    )

    make_ribbon_vol = pe.MapNode(
        fsl.maths.MultiImageMaths(op_string="-mas %s -mul 255"),
        iterfield=["in_file", "operand_files"],
        name="make_ribbon_vol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    bin_ribbon_vol = pe.MapNode(
        fsl.maths.UnaryMaths(operation="bin"),
        iterfield=["in_file"],
        name="bin_ribbon_vol",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    split_squeeze_ribbon_vol = pe.Node(
        niu.Split(splits=[1, 1], squeeze=True),
        name="split_squeeze_ribbon",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
        run_without_submitting=True,
    )

    combine_ribbon_vol_hemis = pe.Node(
        fsl.maths.BinaryMaths(operation="add"),
        name="combine_ribbon_vol_hemis",
        mem_gb=DEFAULT_MEMORY_MIN_GB,
    )

    # make HCP-style ribbon volume in T1w space
    workflow.connect(
        [
            (inputnode, select_wm, [("surfaces", "inlist")]),
            (inputnode, select_pial, [("surfaces", "inlist")]),
            (inputnode, select_midthick, [("surfaces", "inlist")]),
            (select_wm, create_wm_distvol, [(("out", _sorted_by_basename), "surf_file")]),
            (inputnode, create_wm_distvol, [("t1w_mask", "ref_file")]),
            (select_pial, create_pial_distvol, [(("out", _sorted_by_basename), "surf_file")]),
            (inputnode, create_pial_distvol, [("t1w_mask", "ref_file")]),
            (create_wm_distvol, thresh_wm_distvol, [("out_file", "in_file")]),
            (create_pial_distvol, uthresh_pial_distvol, [("out_file", "in_file")]),
            (thresh_wm_distvol, bin_wm_distvol, [("out_file", "in_file")]),
            (uthresh_pial_distvol, bin_pial_distvol, [("out_file", "in_file")]),
            (bin_wm_distvol, split_wm_distvol, [("out_file", "inlist")]),
            (split_wm_distvol, merge_wm_distvol_no_flatten, [("out1", "in1")]),
            (split_wm_distvol, merge_wm_distvol_no_flatten, [("out2", "in2")]),
            (bin_pial_distvol, make_ribbon_vol, [("out_file", "in_file")]),
            (merge_wm_distvol_no_flatten, make_ribbon_vol, [("out", "operand_files")]),
            (make_ribbon_vol, bin_ribbon_vol, [("out_file", "in_file")]),
            (bin_ribbon_vol, split_squeeze_ribbon_vol, [("out_file", "inlist")]),
            (split_squeeze_ribbon_vol, combine_ribbon_vol_hemis, [("out1", "in_file")]),
            (split_squeeze_ribbon_vol, combine_ribbon_vol_hemis, [("out2", "operand_file")]),
            (combine_ribbon_vol_hemis, outputnode, [("out_file", "anat_ribbon")]),
        ]
    )
    return workflow


def init_morph_grayords_wf(
    grayord_density: ty.Literal['91k', '170k'],
    name: str = "bold_grayords_wf",
):
    """
    Sample Grayordinates files onto the fsLR atlas.

    Outputs are in CIFTI2 format.

    Workflow Graph
        .. workflow::
            :graph2use: colored
            :simple_form: yes

            from smriprep.workflows.surfaces import init_morph_grayords_wf
            wf = init_morph_grayords_wf(grayord_density="91k")

    Parameters
    ----------
    grayord_density : :obj:`str`
        Either `91k` or `170k`, representing the total of vertices or *grayordinates*.
    name : :obj:`str`
        Unique name for the subworkflow (default: ``"bold_grayords_wf"``)

    Inputs
    ------
    subject_id : :obj:`str`
        FreeSurfer subject ID
    subjects_dir : :obj:`str`
        FreeSurfer SUBJECTS_DIR

    Outputs
    -------
    cifti_morph : :obj:`list` of :obj:`str`
        Paths of CIFTI dscalar files
    cifti_metadata : :obj:`list` of :obj:`str`
        Paths to JSON files containing metadata corresponding to ``cifti_morph``

    """
    import templateflow.api as tf
    from niworkflows.engine.workflows import LiterateWorkflow as Workflow
    from smriprep.interfaces.cifti import GenerateDScalar

    workflow = Workflow(name=name)
    workflow.__desc__ = f"""\
*Grayordinate* "dscalar" files [@hcppipelines] containing {grayord_density} samples were
also generated using the highest-resolution ``fsaverage`` as an intermediate standardized
surface space.
"""

    fslr_density = "32k" if grayord_density == "91k" else "59k"

    inputnode = pe.Node(
        niu.IdentityInterface(fields=["subject_id", "subjects_dir"]),
        name="inputnode",
    )

    outputnode = pe.Node(
        niu.IdentityInterface(fields=["cifti_morph", "cifti_metadata"]),
        name="outputnode",
    )

    get_surfaces = pe.Node(nio.FreeSurferSource(), name="get_surfaces")

    surfmorph_list = pe.Node(
        niu.Merge(3, ravel_inputs=True),
        name="surfmorph_list",
        run_without_submitting=True,
    )

    surf2surf = pe.MapNode(
        fs.SurfaceTransform(target_subject="fsaverage", target_type="gii"),
        iterfield=["source_file", "hemi"],
        name="surf2surf",
        mem_gb=0.01,
    )
    surf2surf.inputs.hemi = ["lh", "rh"] * 3

    # Setup Workbench command. LR ordering for hemi can be assumed, as it is imposed
    # by the iterfield of the MapNode in the surface sampling workflow above.
    resample = pe.MapNode(
        wb.MetricResample(method="ADAP_BARY_AREA", area_metrics=True),
        name="resample",
        iterfield=[
            "in_file",
            "out_file",
            "new_sphere",
            "new_area",
            "current_sphere",
            "current_area",
        ],
    )
    resample.inputs.current_sphere = [
        str(
            tf.get(
                "fsaverage",
                hemi=hemi,
                density="164k",
                desc="std",
                suffix="sphere",
                extension=".surf.gii",
            )
        )
        for hemi in "LR"
    ] * 3
    resample.inputs.current_area = [
        str(
            tf.get(
                "fsaverage",
                hemi=hemi,
                density="164k",
                desc="vaavg",
                suffix="midthickness",
                extension=".shape.gii",
            )
        )
        for hemi in "LR"
    ] * 3
    resample.inputs.new_sphere = [
        str(
            tf.get(
                "fsLR",
                space="fsaverage",
                hemi=hemi,
                density=fslr_density,
                suffix="sphere",
                extension=".surf.gii",
            )
        )
        for hemi in "LR"
    ] * 3
    resample.inputs.new_area = [
        str(
            tf.get(
                "fsLR",
                hemi=hemi,
                density=fslr_density,
                desc="vaavg",
                suffix="midthickness",
                extension=".shape.gii",
            )
        )
        for hemi in "LR"
    ] * 3
    resample.inputs.out_file = [
        f"space-fsLR_hemi-{h}_den-{grayord_density}_{morph}.shape.gii"
        # Order: curv-L, curv-R, sulc-L, sulc-R, thickness-L, thickness-R
        for morph in ('curv', 'sulc', 'thickness')
        for h in "LR"
    ]

    gen_cifti = pe.MapNode(
        GenerateDScalar(
            grayordinates=grayord_density,
        ),
        iterfield=['scalar_name', 'scalar_surfs'],
        name="gen_cifti",
    )
    gen_cifti.inputs.scalar_name = ['curv', 'sulc', 'thickness']

    # fmt: off
    workflow.connect([
        (inputnode, get_surfaces, [
            ('subject_id', 'subject_id'),
            ('subjects_dir', 'subjects_dir'),
        ]),
        (inputnode, surf2surf, [
            ('subject_id', 'source_subject'),
            ('subjects_dir', 'subjects_dir'),
        ]),
        (get_surfaces, surfmorph_list, [
            (('curv', _sorted_by_basename), 'in1'),
            (('sulc', _sorted_by_basename), 'in2'),
            (('thickness', _sorted_by_basename), 'in3'),
        ]),
        (surfmorph_list, surf2surf, [('out', 'source_file')]),
        (surf2surf, resample, [('out_file', 'in_file')]),
        (resample, gen_cifti, [
            (("out_file", _collate), "scalar_surfs")]),
        (gen_cifti, outputnode, [("out_file", "cifti_morph"),
                                 ("out_metadata", "cifti_metadata")]),
    ])
    # fmt: on

    return workflow


def _check_cw256(in_files, default_flags):
    import numpy as np
    from nibabel.funcs import concat_images

    if isinstance(in_files, str):
        in_files = [in_files]
    summary_img = concat_images(in_files)
    fov = np.array(summary_img.shape[:3]) * summary_img.header.get_zooms()[:3]
    flags = list(default_flags)
    if np.any(fov > 256):
        flags.append("-cw256")
    return flags


def _sorted_by_basename(inlist):
    from os.path import basename

    return sorted(inlist, key=lambda x: str(basename(x)))


def _collate(files):
    return [files[i:i + 2] for i in range(0, len(files), 2)]
