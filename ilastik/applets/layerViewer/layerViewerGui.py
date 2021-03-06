import numpy

from PyQt4.QtCore import QRectF, Qt
from PyQt4.QtGui import *
from PyQt4 import uic

from volumina.api import LazyflowSource, NormalizingSource, GrayscaleLayer, RGBALayer, \
                         AlphaModulatedLayer, LayerStackModel, VolumeEditor

from lazyflow.graph import OperatorWrapper
from lazyflow.operators import OpSingleChannelSelector, Op1ToMulti

import os
from ilastik.utility import bind
from ilastik.utility.gui import ThreadRouter, threadRouted, ShortcutManager

from volumina.adaptors import Op5ifyer

from volumina.clickReportingInterpreter import ClickReportingInterpreter

import logging
logger = logging.getLogger(__name__)
traceLogger = logging.getLogger('TRACE.' + __name__)
from lazyflow.tracer import traceLogged, Tracer

class LayerViewerGui(QMainWindow):
    """
    Implements an applet GUI whose central widget is a VolumeEditor 
      and whose layer controls simply contains a layer list widget.
    Intended to be used as a subclass for applet GUI objects.
    
    Provides: - Central widget (viewer)
              - View Menu
              - Layer controls
              
    Does NOT provide an applet drawer widget.
    """
    ###########################################
    ### AppletGuiInterface Concrete Methods ###
    ###########################################
    
    def centralWidget( self ):
        return self

    def appletDrawers(self):
        return [('Viewer', QWidget())]

    def menus( self ):
        return [self.menuView] # From the .ui file

    def viewerControlWidget(self):
        return self.__viewerControlWidget

    def setImageIndex(self, index):
        self._setImageIndex(index)
        
    def reset(self):
        # Remove all layers
        self.layerstack.clear()
    
    ###########################################
    ###########################################

    def operatorForCurrentImage(self):
        try:
            return self.topLevelOperator[self.imageIndex]
        except IndexError:
            return None

    @traceLogged(traceLogger)
    def __init__(self, topLevelOperator):
        """
        Args:
            observedSlots   - A list of slots that we'll listen for changes on.
                               Each must be a multislot with level=1 or level=2.
                               The first index in the multislot is the image index. 
        """
        super(LayerViewerGui, self).__init__()

        self.threadRouter = ThreadRouter(self) # For using @threadRouted

        self.topLevelOperator = topLevelOperator

        observedSlots = []

        for slot in topLevelOperator.inputs.values() + topLevelOperator.outputs.values():
            if slot.level == 1 or slot.level == 2:
                observedSlots.append(slot)
        
        self.observedSlots = []        
        for slot in observedSlots:
            if slot.level == 1:
                # The user gave us a slot that is indexed as slot[image]
                # Wrap the operator so it has the right level.  Indexed as: slot[image][0]
                opPromoteInput = OperatorWrapper( Op1ToMulti, graph=slot.operator.graph )
                opPromoteInput.Input.connect(slot)
                slot = opPromoteInput.Outputs

            # Each slot should now be indexed as slot[image][sub_index]
            assert slot.level == 2
            self.observedSlots.append( slot )

        self.layerstack = LayerStackModel()

        self.initAppletDrawerUi() # Default implementation loads a blank drawer.
        self.initCentralUic()
        self.__viewerControlWidget = None
        self.initViewerControlUi()
        
        self.initEditor()
        
        self.imageIndex = -1
        self.lastUpdateImageIndex = -1
        
        def handleDatasetInsertion(slot, imageIndex):
            if self.imageIndex == -1 and self.areProvidersInSync():
                self.setImageIndex( imageIndex )
        
        for provider in self.observedSlots:
            provider.notifyInserted( bind( handleDatasetInsertion ) )
        
        def handleDatasetRemoval(slot, index, finalsize):
            if finalsize == 0:
                # Clear everything
                self.setImageIndex(-1)
            elif index == self.imageIndex:
                # Our currently displayed image is being removed.
                # Switch to the first image (unless that's the one being removed!)
                newIndex = 0
                if index == newIndex:
                    newIndex = 1
                self.setImageIndex(newIndex)
            
        for provider in self.observedSlots:
            provider.notifyRemove( bind( handleDatasetRemoval ) )

    def setupLayers( self, currentImageIndex ):
        layers = []
        for multiImageSlot in self.observedSlots:
            if 0 <= currentImageIndex < len(multiImageSlot):
                multiLayerSlot = multiImageSlot[currentImageIndex]
                for j, slot in enumerate(multiLayerSlot):
                    if slot.ready():
                        layer = self.createStandardLayerFromSlot(slot)
                        layer.name = multiImageSlot.name + " " + str(j)
                        layers.append(layer)        
        return layers

    @traceLogged(traceLogger)
    def _setImageIndex(self, imageIndex):
        if self.imageIndex != -1:
            for provider in self.observedSlots:
                # We're switching datasets.  Unsubscribe from the old one's notifications.
                provider[self.imageIndex].unregisterInserted( bind(self.handleLayerInsertion) )
                provider[self.imageIndex].unregisterRemove( bind(self.handleLayerRemoval) )

        self.imageIndex = imageIndex
        
        # Don't repopulate the GUI if there isn't a current dataset.  Stop now. 
        if imageIndex is -1:
            self.layerstack.clear()
            return

        # Update the GUI for all layers in the current dataset
        self.updateAllLayers()

        # For layers that already exist, subscribe to ready notifications
        for provider in self.observedSlots:
            for slotIndex, slot in enumerate(provider):
                slot.notifyReady( bind(self.updateAllLayers) )
                slot.notifyUnready( bind(self.updateAllLayers) )
        
        # Make sure we're notified if a layer is inserted in the future so we can subscribe to its ready notifications
        for provider in self.observedSlots:
            if self.imageIndex < len(provider):
                provider[self.imageIndex].notifyInserted( bind(self.handleLayerInsertion) )
                provider[self.imageIndex].notifyRemoved( bind(self.handleLayerRemoval) )

    def handleLayerInsertion(self, slot, slotIndex):
        """
        The multislot providing our layers has a new item.
        Make room for it in the layer GUI and subscribe to updates.
        """
        with Tracer(traceLogger):
            # When the slot is ready, we'll replace the blank layer with real data
            slot[slotIndex].notifyReady( bind(self.updateAllLayers) )
            slot[slotIndex].notifyUnready( bind(self.updateAllLayers) )
    
    def handleLayerRemoval(self, slot, slotIndex):
        """
        An item is about to be removed from the multislot that is providing our layers.
        Remove the layer from the GUI.
        """
        with Tracer(traceLogger):
            self.updateAllLayers()

    def generateAlphaModulatedLayersFromChannels(self, slot):
        # TODO
        assert False

    @traceLogged(traceLogger)
    def createStandardLayerFromSlot(self, slot, lastChannelIsAlpha=False):
        """
        Generate a volumina layer using the given slot.
        Choose between grayscale or RGB depending on the number of channels.
        """
        def getRange(meta):
            if 'drange' in meta:
                return meta.drange
            if numpy.issubdtype(meta.dtype, numpy.integer):
                # We assume that ints range up to their max possible value,
                return (0, numpy.iinfo( meta.dtype ).max)
            else:
                # If we don't know the range of the data, create a layer that is auto-normalized.
                # See volumina.pixelpipeline.datasources for details.
                return 'autoPercentiles'

        # Examine channel dimension to determine Grayscale vs. RGB
        shape = slot.meta.shape
        normalize = getRange(slot.meta)
        try:
            channelAxisIndex = slot.meta.axistags.index('c')
            #assert channelAxisIndex < len(slot.meta.axistags), \
            #    "slot %s has shape = %r, axistags = %r, but no channel dimension" \
            #    % (slot.name, slot.meta.shape, slot.meta.axistags)
            numChannels = shape[channelAxisIndex]
        except:
            numChannels = 1
        
        if lastChannelIsAlpha:
            assert numChannels <= 4, "Can't display a standard layer with more than four channels (with alpha).  Your image has {} channels.".format(numChannels)
        else:
            assert numChannels <= 3, "Can't display a standard layer with more than three channels (with no alpha).  Your image has {} channels.".format(numChannels)

        if numChannels == 1:
            assert not lastChannelIsAlpha, "Can't have an alpha channel if there is no color channel"
            source = LazyflowSource(slot)
            normSource = NormalizingSource( source, bounds=normalize )
            return GrayscaleLayer(normSource)

        assert numChannels > 2 or (numChannels == 2 and not lastChannelIsAlpha)
        redProvider = OpSingleChannelSelector(graph=slot.graph)
        redProvider.Input.connect(slot)
        redProvider.Index.setValue( 0 )
        redSource = LazyflowSource( redProvider.Output )
        redNormSource = NormalizingSource( redSource, bounds=normalize )
        
        greenProvider = OpSingleChannelSelector(graph=slot.graph)
        greenProvider.Input.connect(slot)
        greenProvider.Index.setValue( 1 )
        greenSource = LazyflowSource( greenProvider.Output )
        greenNormSource = NormalizingSource( greenSource, bounds=normalize )
                        
        blueNormSource = None
        if numChannels > 3 or (numChannels == 3 and not lastChannelIsAlpha):
            blueProvider = OpSingleChannelSelector(graph=slot.graph)
            blueProvider.Input.connect(slot)
            blueProvider.Index.setValue( 2 )
            blueSource = LazyflowSource( blueProvider.Output )
            blueNormSource = NormalizingSource( blueSource, bounds=normalize )

        alphaNormSource = None
        if lastChannelIsAlpha:
            alphaProvider = OpSingleChannelSelector(graph=slot.graph)
            alphaProvider.Input.connect(slot)
            alphaProvider.Index.setValue( numChannels-1 )
            alphaSource = LazyflowSource( alphaProvider.Output )
            alphaNormSource = NormalizingSource( alphaSource, bounds=normalize )
        
        layer = RGBALayer( red=redNormSource, green=greenNormSource, blue=blueNormSource, alpha=alphaNormSource )
        return layer

    @traceLogged(traceLogger)
    def areProvidersInSync(self):
        """
        When an image is appended to the workflow, not all slots are resized simultaneously.
        We should avoid calling setupLayers() until all the slots have been resized with the new image.
        """
        try:
            numImages = len(self.observedSlots[0])
        except IndexError: # observedSlots is empty
            pass

        inSync = True
        for slot in self.observedSlots:
            # Check each slot for out-of-sync status except:
            # - slots that are optional and unconnected
            # - slots that are not images (e.g. a classifier or other object)
            if not (slot._optional and slot.partner is None):
                if len(slot) == 0:
                    inSync = False
                    break
                elif len(slot[0]) > 0 and slot[0][0].meta.axistags is not None:
                    inSync &= (len(slot) == numImages)
        return inSync

    @traceLogged(traceLogger)
    @threadRouted
    def updateAllLayers(self):
        # Check to make sure all layers are in sync
        # (During image insertions, outputs are resized one at a time.)
        if not self.areProvidersInSync():
            return

        if self.imageIndex >= 0:        
            # Ask the subclass for the updated layer list
            newGuiLayers = self.setupLayers(self.imageIndex)
        else:
            newGuiLayers = []
            
        newNames = set(l.name for l in newGuiLayers)
        if len(newNames) != len(newGuiLayers):
            msg = "All layers must have unique names.\n"
            msg += "You're attempting to use these layer names:\n"
            msg += [l.name for l in newGuiLayers]
            raise RuntimeError()

        # Copy the old visibilities and opacities
        if self.imageIndex != self.lastUpdateImageIndex:
            existing = {l.name : l for l in self.layerstack}
            for layer in newGuiLayers:
                if layer.name in existing.keys():
                    layer.visible = existing[layer.name].visible
                    layer.opacity = existing[layer.name].opacity

            # Clear all existing layers.
            self.layerstack.clear()
            self.lastUpdateImageIndex = self.imageIndex

            # Zoom at a 1-1 scale to avoid loading big datasets entirely...
            for view in self.editor.imageViews:
                view.doScaleTo(1)
        
        # If the datashape changed, tell the editor
        newDataShape = self.determineDatashape()
        if newDataShape is not None and self.editor.dataShape != newDataShape:
            self.editor.dataShape = newDataShape
            # Find the xyz midpoint
            midpos5d = [x/2 for x in newDataShape]
            midpos3d = midpos5d[1:4]
            
            # Start in the center of the volume
            self.editor.posModel.slicingPos = midpos3d
            self.editor.navCtrl.panSlicingViews( midpos3d, [0,1,2] )
            
            # If one of the xyz dimensions is 1, the data is 2d.
            singletonDims = filter( lambda (i,dim): dim == 1, enumerate(newDataShape[1:4]) )
            if len(singletonDims) == 1:
                # Maximize the slicing view for this axis
                axis = singletonDims[0][0]
                self.volumeEditorWidget.quadview.ensureMaximized(axis)

        # Old layers are deleted if
        # (1) They are not in the new set or
        # (2) Their data has changed
        for index, oldLayer in reversed(list(enumerate(self.layerstack))):
            if oldLayer.name not in newNames:
                needDelete = True
            else:
                newLayer = filter(lambda l: l.name == oldLayer.name, newGuiLayers)[0]
                needDelete = (newLayer.datasources != oldLayer.datasources)

            if needDelete:
                layer = self.layerstack[index]
                if hasattr(layer, 'shortcutRegistration'):
                    obsoleteShortcut = layer.shortcutRegistration[2]
                    obsoleteShortcut.setEnabled(False)
                    ShortcutManager().unregister( obsoleteShortcut )
                self.layerstack.selectRow(index)
                self.layerstack.deleteSelected()

        # Insert all layers that aren't already in the layerstack
        # (Identified by the name attribute)
        existingNames = set(l.name for l in self.layerstack)
        for index, layer in enumerate(newGuiLayers):
            if layer.name not in existingNames:
                # Insert new
                self.layerstack.insert( index, layer )
                
                # If this layer has an associated shortcut, register it with the shortcut manager
                if hasattr(layer, 'shortcutRegistration'):
                    ShortcutManager().register( *layer.shortcutRegistration )
            else:
                # Clean up the layer instance that the client just gave us.
                # We don't want to use it.
                if hasattr(layer, 'shortcutRegistration'):
                    shortcut = layer.shortcutRegistration[2]
                    shortcut.setEnabled(False)

                # Move existing layer to the correct positon
                stackIndex = self.layerstack.findMatchingIndex(lambda l: l.name == layer.name)
                self.layerstack.selectRow(stackIndex)
                while stackIndex > index:
                    self.layerstack.moveSelectedUp()
                    stackIndex -= 1
                while stackIndex < index:
                    self.layerstack.moveSelectedDown()
                    stackIndex += 1
                
    @traceLogged(traceLogger)
    def determineDatashape(self):
        if self.imageIndex < 0:
            return None

        newDataShape = None
        for provider in self.observedSlots:
            if self.imageIndex < len(provider):
                for i, slot in enumerate(provider[self.imageIndex]):
                    if newDataShape is None and slot.ready() and slot.meta.axistags is not None:
                        # Use an Op5ifyer adapter to transpose the shape for us.
                        op5 = Op5ifyer( graph=slot.graph )
                        op5.input.connect( slot )
                        newDataShape = op5.output.meta.shape
    
                        # We just needed the operator to determine the transposed shape.
                        # Disconnect it so it can be garbage collected.
                        op5.input.disconnect()

        if newDataShape is not None:
            # For now, this base class combines multi-channel images into a single layer,
            # So, we want the volume editor to behave as though there is only one channel 
            newDataShape = newDataShape[:-1] + (1,)
        return newDataShape

    @traceLogged(traceLogger)
    def initViewerControlUi(self):
        """
        Load the viewer controls GUI, which appears below the applet bar.
        In our case, the viewer control GUI consists mainly of a layer list.
        """
        localDir = os.path.split(__file__)[0]
        self.__viewerControlWidget = uic.loadUi(localDir + "/viewerControls.ui")

    @traceLogged(traceLogger)
    def initAppletDrawerUi(self):
        """
        By default, this base class provides a blank applet drawer.
        Override this in a subclass to get a real applet drawer.
        """
        # Load the ui file (find it in our own directory)
        localDir = os.path.split(__file__)[0]
        self._drawer = uic.loadUi(localDir+"/drawer.ui")
    
    def getAppletDrawerUi(self):
        return self._drawer

    @traceLogged(traceLogger)
    def initCentralUic(self):
        """
        Load the GUI from the ui file into this class and connect it with event handlers.
        """
        # Load the ui file into this class (find it in our own directory)
        localDir = os.path.split(__file__)[0]
        uic.loadUi(localDir+"/centralWidget.ui", self)

        # Menu is specified in a separate ui file with a dummy window
        self.menuGui = uic.loadUi(localDir+"/menu.ui") # Save as member so it doesn't get picked up by GC
        self.menuBar = self.menuGui.menuBar
        self.menuView = self.menuGui.menuView
            
        def toggleDebugPatches(show):
            self.editor.showDebugPatches = show
        
        def setCacheSize( cache_size ):
            dlg = QDialog(self)
            layout = QHBoxLayout()
            layout.addWidget( QLabel("Cached Slices Per View:") )

            cache_size = [self.editor.cacheSize]
            def parseCacheSize( strSize ):
                try:
                    cache_size[0] = int(strSize)
                except:
                    pass
                
            edit = QLineEdit( str(cache_size[0]), parent=dlg )
            edit.textChanged.connect( parseCacheSize )
            layout.addWidget( edit )
            okButton = QPushButton( "OK", parent=dlg )
            okButton.clicked.connect( dlg.accept )
            layout.addWidget( okButton )
            dlg.setLayout( layout )
            dlg.setModal(True)
            dlg.exec_()
            self.editor.cacheSize = cache_size[0]
        
        def fitToScreen():
            shape = self.editor.posModel.shape
            for i, v in enumerate(self.editor.imageViews):
                s = list(shape)
                del s[i]
                v.changeViewPort(v.scene().data2scene.mapRect(QRectF(0,0,*s)))  
                
        def fitImage():
            if hasattr(self.editor, '_lastImageViewFocus'):
                self.editor.imageViews[self.editor._lastImageViewFocus].fitImage()
                
        def restoreImageToOriginalSize():
            if hasattr(self.editor, '_lastImageViewFocus'):
                self.editor.imageViews[self.editor._lastImageViewFocus].doScaleTo()
                    
        def rubberBandZoom():
            if hasattr(self.editor, '_lastImageViewFocus'):
                if not self.editor.imageViews[self.editor._lastImageViewFocus]._isRubberBandZoom:
                    self.editor.imageViews[self.editor._lastImageViewFocus]._isRubberBandZoom = True
                    self.editor.imageViews[self.editor._lastImageViewFocus]._cursorBackup = self.editor.imageViews[self.editor._lastImageViewFocus].cursor()
                    self.editor.imageViews[self.editor._lastImageViewFocus].setCursor(Qt.CrossCursor)
                else:
                    self.editor.imageViews[self.editor._lastImageViewFocus]._isRubberBandZoom = False
                    self.editor.imageViews[self.editor._lastImageViewFocus].setCursor(self.editor.imageViews[self.editor._lastImageViewFocus]._cursorBackup)
                
        def hideHud():
            hide = not self.editor.imageViews[0]._hud.isVisible()
            for i, v in enumerate(self.editor.imageViews):
                v.setHudVisible(hide)
                
        def toggleSelectedHud():
            if hasattr(self.editor, '_lastImageViewFocus'):
                self.editor.imageViews[self.editor._lastImageViewFocus].toggleHud()
                
        def centerAllImages():
            for i, v in enumerate(self.editor.imageViews):
                v.centerImage()
                
        def centerImage():
            if hasattr(self.editor, '_lastImageViewFocus'):
                self.editor.imageViews[self.editor._lastImageViewFocus].centerImage()
                self.actionOnly_for_current_view.setEnabled(True)
        
        self.menuGui.actionCenterAllImages.triggered.connect(centerAllImages)
        self.menuGui.actionCenterImage.triggered.connect(centerImage)
        self.menuGui.actionToggleAllHuds.triggered.connect(hideHud)
        self.menuGui.actionToggleSelectedHud.triggered.connect(toggleSelectedHud)
        self.menuGui.actionShowDebugPatches.toggled.connect(toggleDebugPatches)
        self.menuGui.actionFitToScreen.triggered.connect(fitToScreen)
        self.menuGui.actionFitImage.triggered.connect(fitImage)
        self.menuGui.actionReset_zoom.triggered.connect(restoreImageToOriginalSize)
        self.menuGui.actionRubberBandZoom.triggered.connect(rubberBandZoom)        
        self.menuGui.actionSetCacheSize.triggered.connect(setCacheSize)
                
    @traceLogged(traceLogger)
    def initEditor(self):
        """
        Initialize the Volume Editor GUI.
        """
        self.editor = VolumeEditor(self.layerstack)

        # Replace the editor's navigation interpreter with one that has extra functionality
        self.clickReporter = ClickReportingInterpreter( self.editor.navInterpret, self.editor.posModel )
        self.editor.setNavigationInterpreter( self.clickReporter )
        self.clickReporter.rightClickReceived.connect( self._handleEditorRightClick )
        self.clickReporter.leftClickReceived.connect( self._handleEditorLeftClick )

        self.editor.newImageView2DFocus.connect(self.setIconToViewMenu)
        self.editor.setInteractionMode( 'navigation' )
        self.volumeEditorWidget.init(self.editor)
        
        # The editor's layerstack is in charge of which layer movement buttons are enabled
        model = self.editor.layerStack

        if self.__viewerControlWidget is not None:
            model.canMoveSelectedUp.connect(self.__viewerControlWidget.UpButton.setEnabled)
            model.canMoveSelectedDown.connect(self.__viewerControlWidget.DownButton.setEnabled)
            model.canDeleteSelected.connect(self.__viewerControlWidget.DeleteButton.setEnabled)     

            # Connect our layer movement buttons to the appropriate layerstack actions
            self.__viewerControlWidget.layerWidget.init(model)
            self.__viewerControlWidget.UpButton.clicked.connect(model.moveSelectedUp)
            self.__viewerControlWidget.DownButton.clicked.connect(model.moveSelectedDown)
            self.__viewerControlWidget.DeleteButton.clicked.connect(model.deleteSelected)
        
        self.editor._lastImageViewFocus = 0

    @traceLogged(traceLogger)
    def setIconToViewMenu(self):
        """
        In the "Only for Current View" menu item of the View menu, 
        show the user which axis is the current one by changing the menu item icon.
        """
        self.actionOnly_for_current_view.setIcon(QIcon(self.editor.imageViews[self.editor._lastImageViewFocus]._hud.axisLabel.pixmap()))

    @traceLogged(traceLogger)
    def _convertPositionToDataSpace(self, voluminaPosition):
        taggedPosition = {k:p for k,p in zip('txyzc', voluminaPosition)}
        
        # Find the first lazyflow layer in the stack
        # We assume that all lazyflow layers have the same axistags
        dataTags = None
        for layer in self.layerstack:
            for datasource in layer.datasources:
                if isinstance( datasource, NormalizingSource ):
                    datasource = datasource._rawSource
                if isinstance(datasource, LazyflowSource):
                    dataTags = datasource.dataSlot.meta.axistags
                    if dataTags is not None:
                        break

        assert dataTags is not None, "Can't convert mouse click coordinates from volumina-5d: Could not find a lazyflow data source in any layer."
        position = ()
        for tag in dataTags:
            position += (taggedPosition[tag.key],)
            
        return position
    
    def _handleEditorRightClick(self, position5d, globalWindowCoordinate):
        dataPosition = self._convertPositionToDataSpace(position5d)
        self.handleEditorRightClick(self.imageIndex, dataPosition, globalWindowCoordinate)

    def _handleEditorLeftClick(self, position5d, globalWindowCoordinate):
        dataPosition = self._convertPositionToDataSpace(position5d)
        self.handleEditorLeftClick(self.imageIndex, dataPosition, globalWindowCoordinate)

    def handleEditorRightClick(self, currentImageIndex, position5d, globalWindowCoordinate):
        # Override me
        pass

    def handleEditorLeftClick(self, currentImageIndex, position5d, globalWindowCoordiante):
        # Override me
        pass























