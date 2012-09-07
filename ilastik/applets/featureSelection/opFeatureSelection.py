from lazyflow.graph import Operator, InputSlot, OutputSlot, MultiOutputSlot

from lazyflow.operators import OpPixelFeaturesPresmoothed, OpSlicedBlockedArrayCache, OpMultiArraySlicer2

class OpFeatureSelection(Operator):
    """
    The top-level operator for the feature selection applet.
    """
    name = "OpFeatureSelection"
    category = "Top-level"

    # Multiple input images
    InputImage = InputSlot()

    # The following input slots are applied uniformly to all input images
    Scales = InputSlot() # The list of possible scales to use when computing features
    FeatureIds = InputSlot() # The list of features to compute
    SelectionMatrix = InputSlot() # A matrix of bools indicating which features to output.
                         # The matrix rows correspond to feature types in the order specified by the FeatureIds input.
                         #  (See OpPixelFeaturesPresmoothed for the available feature types.)
                         # The matrix columns correspond to the scales provided in the Scales input,
                         #  which requires that the number of matrix columns must match len(Scales.value)
    
    # Features are presented in the channels of the output image
    # Output can be optionally accessed via an internal cache.
    # (Training a classifier benefits from caching, but predicting with an existing classifier does not.)
    OutputImage = OutputSlot()
    CachedOutputImage = OutputSlot()

    FeatureLayers = MultiOutputSlot() # For the GUI, we also provide each feature as a separate slot in this multislot
    
    def __init__(self, *args, **kwargs):
        super(OpFeatureSelection, self).__init__(*args, **kwargs)

        # Two internal operators: features and cache
        self.opPixelFeatures = OpPixelFeaturesPresmoothed(parent=self)
        self.opPixelFeatureCache = OpSlicedBlockedArrayCache(parent=self)
        self.opPixelFeatureCache.name = "opPixelFeatureCache"

        # Connect the cache to the feature output
        self.opPixelFeatureCache.Input.connect(self.opPixelFeatures.Output)
        self.opPixelFeatureCache.fixAtCurrent.setValue(False)

        # Connect our internal operators to our external inputs 
        self.opPixelFeatures.Scales.connect( self.Scales )
        self.opPixelFeatures.FeatureIds.connect( self.FeatureIds )
        self.opPixelFeatures.Matrix.connect( self.SelectionMatrix )
        self.opPixelFeatures.Input.connect( self.InputImage )
        
        # Connect our external outputs to our internal operators
        self.OutputImage.connect( self.opPixelFeatures.Output )
        self.CachedOutputImage.connect( self.opPixelFeatureCache.Output )        
        self.FeatureLayers.connect( self.opPixelFeatures.Features )

    def setupOutputs(self):        
        # We choose block shapes that have only 1 channel because the channels may be 
        #  coming from different features (e.g different filters) and probably shouldn't be cached together.
        blockDimsX = { 't' : (1,1),
                       'z' : (32,128),
                       'y' : (32,128),
                       'x' : (1,1),
                       'c' : (1,1) }

        blockDimsY = { 't' : (1,1),
                       'z' : (32,128),
                       'y' : (1,1),
                       'x' : (32,128),
                       'c' : (1,1) }

        blockDimsZ = { 't' : (1,1),
                       'z' : (1,1),
                       'y' : (32,128),
                       'x' : (32,128),
                       'c' : (1,1) }

        axisOrder = [ tag.key for tag in self.InputImage.meta.axistags ]
        innerBlockShapeX = tuple( blockDimsX[k][0] for k in axisOrder )
        outerBlockShapeX = tuple( blockDimsX[k][1] for k in axisOrder )

        innerBlockShapeY = tuple( blockDimsY[k][0] for k in axisOrder )
        outerBlockShapeY = tuple( blockDimsY[k][1] for k in axisOrder )

        innerBlockShapeZ = tuple( blockDimsZ[k][0] for k in axisOrder )
        outerBlockShapeZ = tuple( blockDimsZ[k][1] for k in axisOrder )

        # Configure the cache        
        self.opPixelFeatureCache.innerBlockShape.setValue( (innerBlockShapeX, innerBlockShapeY, innerBlockShapeZ) )
        self.opPixelFeatureCache.outerBlockShape.setValue( (outerBlockShapeX, outerBlockShapeY, outerBlockShapeZ) )


    def propagateDirty(self, inputSlot, roi):
        # Output slots are directly connected to internal operators
        print "Propagate dirty called for slot: {}, roi: {}".format(inputSlot.name, roi)



















