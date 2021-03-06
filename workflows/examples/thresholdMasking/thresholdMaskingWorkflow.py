from ilastik.workflow import Workflow

from lazyflow.graph import Graph

from ilastik.applets.dataSelection import DataSelectionApplet
from ilastik.applets.thresholdMasking import ThresholdMaskingApplet

class ThresholdMaskingWorkflow(Workflow):
    def __init__(self):
        super(ThresholdMaskingWorkflow, self).__init__()
        self._applets = []

        # Create a graph to be shared by all operators
        graph = Graph()

        # Create applets 
        self.dataSelectionApplet = DataSelectionApplet(graph, "Input Data", "Input Data", supportIlastik05Import=True, batchDataGui=False)
        self.thresholdMaskingApplet = ThresholdMaskingApplet(graph, "Thresholding", "Thresholding Stage 1")

        self._applets.append( self.dataSelectionApplet )
        self._applets.append( self.thresholdMaskingApplet )
        
        # Connect top-level operators
        self.thresholdMaskingApplet.topLevelOperator.InputImage.connect( self.dataSelectionApplet.topLevelOperator.Image )

    @property
    def applets(self):
        return self._applets

    @property
    def imageNameListSlot(self):
        return self.dataSelectionApplet.topLevelOperator.ImageName
