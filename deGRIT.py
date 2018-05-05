#! python3

# DEGRIT (DEtection and Genomic Rectification of Indels using Transcripts)

# This program attempts to improve (or de-grit) a genome assembly through rectification of indel errors
# utilising RNAseq transcripts. In order to reduce the false positive rate, changes are 
# limited specifically to regions that represent currently annotated genes. An additional
# "gene rescuing" module is optionally available to allow correction of indels in regions 
# that are not part of currently annotated genes; this behaviour is expected to be slightly
# more error prone, but in practice I have not found this module to incorrectly modify the genome
# and thus it may be considered part of the default behavior of this program. Through alignment
# of the transcript and genome sequences, we can find the precise locations where indel errors
# occur and correct these in the genomic sequence, enabling reannotation to occur.

# Load packages
import re, os, argparse, copy, pickle, warnings, shutil
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.Alphabet import generic_dna
from skbio.alignment import StripedSmithWaterman

### Various functions to perform operations throughout the program
def reverse_comp(seq):
        reversedSeq = seq[::-1].lower()
        # Complement characters
        reversedSeq = reversedSeq.replace('a', 'T')
        reversedSeq = reversedSeq.replace('t', 'A')
        reversedSeq = reversedSeq.replace('c', 'G')
        reversedSeq = reversedSeq.replace('g', 'C')
        return reversedSeq

def gmap_exon_finder(gmapLoc, model, coordIndex, processType):
        dictEntries = []
        start = int(model[0][coordIndex].split('-')[0])
        stop = int(model[0][coordIndex].split('-')[1])
        if processType == 'boundary':
                dictEntries = [gmapLoc[key] for key in gmapLoc if start in key or stop in key]          # Getting start OR stop means we just grab onto anything perfectly matching one of the boundaries
        else:
                dictEntries = [gmapLoc[key] for key in gmapLoc if start in key and stop in key]         # Getting start AND stop means it must equal or encompass our current exon
        dictEntries = copy.deepcopy(dictEntries)                                                        # I'm not 100% sure this is necessary, but I've found deepcopies to be necessary for other functions similar to this
        # Narrow down our dictEntries to hits on the same contig
        for j in range(len(dictEntries)):                                                               # Remember: gmapLoc = [[contigStart, contigStop, transStart, transStop, orient, geneID, identity, contigID]]
                for k in range(len(dictEntries[j])-1, -1, -1):                                          # Loop through in reverse so we can delete entries without messing up the list
                        if dictEntries[j][k][7] != model[2]:
                                del dictEntries[j][k]
        while [] in dictEntries:
                del dictEntries[dictEntries.index([])]
        # Flatten our list
        outList = []
        for entry in dictEntries:
                for subentry in entry:
                        outList.append(subentry)
        # Extra processing if looking at first and last exons
        if processType == 'boundary':
                # Narrow down this list further to make sure we're only holding onto perfect boundary matches
                for n in range(len(outList)-1, -1, -1):
                        if outList[n][0] == start or outList[n][1] == stop:
                                continue
                        else:
                                del outList[n]
        # Sort and return list
        outList.sort(key = lambda x: (int(x[6]), x[2] - x[1]), reverse = True)                          # Provides a sorted list where, at the top, we have the longest and best matching hits
        return outList

def gmap_curate(minCutoff, gmapMatches, model, coordIndex):
        coords = model[0][coordIndex].split('-')
        bestMatches = []
        # Remove spurious matches and detect perfect exon boundary matches
        for x in range(len(gmapMatches)-1,-1,-1):
                """By putting this before the minCutoff we can ensure that, in the case that we have perfect boundary matches but with
                lower identity (like utg103.19) we can prioritise these above 'better' matches which don't respect exon boundaries"""
                if gmapMatches[x][0] == int(coords[0]) and gmapMatches[x][1] == int(coords[1]):
                        bestMatches.append(gmapMatches[x])
                elif gmapMatches[x][6] < minCutoff:
                        del gmapMatches[x]
                   
        # Return the best match if we can or, if not, just return any matches that meet our curation conditions
        if bestMatches == []:
                return gmapMatches
        else:
                """I added in this condition due to situation with utg103.12 where we had two gmapMatches, one was "perfect" (100% identity, exact exon boundary alignment)
                but the other, despite 98% identity, still had a better SSW score simply because it was longer. I could normalise SSW score to handle this, but this is probably
                just as good as it should reduce the computational time of the script by quite a bit.
                Also important consideration: if there is a GMAP alignment which matches this exon's boundaries perfectly, it provides solid evidence that this exon boundary
                should not be changed, so we can limit our consideration to these matches"""
                return bestMatches

def patch_seq_extract(match, model):
        # Transcriptomic records
        transcriptRecord = copy.deepcopy(transRecords[match[5]])
        if match[4] != '+':                                                                             # Put it in the same orientation as the genome sequence [the genome sequence is always + orientation]
                transcriptRecord = transcriptRecord.reverse_complement()
        # Genomic patch (correlating to transcript alignment positions)
        genomePatchRec = genomeRecords[model[2]][int(match[0])-1:int(match[1])]
        return transcriptRecord, genomePatchRec

def ssw(genomePatchRec, transcriptRecord):
        # Perform SSW with scikit.bio implementation
        query = StripedSmithWaterman(str(genomePatchRec.seq))
        alignment = query(str(transcriptRecord.seq))
        genomeAlign = alignment.aligned_query_sequence
        transcriptAlign = alignment.aligned_target_sequence
        # Figure out where we're starting in the genome with this alignment
        startIndex = str(genomePatchRec.seq).find(genomeAlign.replace('-', ''))
        # Figure out if we need downstream processing to identify an indel
        hyphen = 'n'
        if '-' in genomeAlign:
                hyphen = 'y'
        elif '-' in transcriptAlign:
                hyphen = 'y'
        return [transcriptAlign, genomeAlign, hyphen, startIndex, alignment.optimal_alignment_score]

def indel_location(transcriptAlign, genomeAlign, matchStart, model, startIndex, inputVcf, minCutoff):   # This function will check hyphens in the transcript (== deletions in the genome) and hyphens in the genome (== insertion from the transcript)
        # Check if this is likely to be worth bothering
        badChars = ['---', 'n']
        for char in badChars:                                                                           # This is a rough metric, but gap opens larger than three make us wonder whether this transcript does actually originate from the alignment position (maybe it's a paralogue?); in almost all cases, the indel has a length of one. Further, if we have N's in the aligned region then it's likely that the transcript itself is poorly assembled]
                if char in transcriptAlign.lower() or char in genomeAlign.lower():
                        return inputVcf, 0, '.'                                                         # We return 0 since that tells the main part of the script that this hit isn't good enough and to stick to the current model coordinates, the tmpVcf value doesn't matter in this case so just return '.'
        # Process the alignment to find differences
        identical = 0
        tmpVcf = {}                                                                                     # We want to add results into a temporary dictionary because, for sequences which mysteriously do not have good identity, we don't want to save their edit positions
        for x in range(len(transcriptAlign)):
                genomeIndex = matchStart + startIndex + x                                               # This will correspond to the genomic contig index [note that we add startIndex to match[0] because we may have trimmed some of the 5' sequence during SW alignment]
                pair = transcriptAlign[x] + genomeAlign[x]                                              # Note that these are 1-based (it's helpful for me to manually validate code behaviour), so we'll need to account for this behaviour later
                if pair[0] == pair[1]:
                        identical += 1
                elif pair[0] == '-':
                        if model[2] not in tmpVcf:
                                tmpVcf[model[2]] = {genomeIndex: ['.']}
                        else:
                                tmpVcf[model[2]][genomeIndex] = ['.']
                elif pair[1] == '-':
                        if model[2] not in tmpVcf:
                                tmpVcf[model[2]] = {genomeIndex: [pair[0]]}
                        else:
                                tmpVcf[model[2]][genomeIndex] = [pair[0]]
        # Calculate the (rough) identity score between the alignments
        pctIdentity = (identical / len(transcriptAlign)) * 100
        if pctIdentity >= minCutoff:
                # Merge the temporary vcf into the main one
                inputVcf = vcf_merge(inputVcf, tmpVcf)                                                  # If our pctIdentity isn't good enough we make no changes to the inputVcf
        return inputVcf, pctIdentity, tmpVcf                                                            # Return our tmpVcf just for logging purposes

def vcf_edit(tmpVcf, contigID, coordRange):
        # Extract edit positions
        subVcfDict = tmpVcf[contigID]
        tmpVcfList = []
        for key, value in subVcfDict.items():
                if key in coordRange:
                        tmpVcfList.append([key, value[0]])
        tmpVcfList.sort(reverse=True)
        # Edit the genome sequence
        genomeSeq = str(genomeRecords[contigID].seq)[min(coordRange)-1:max(coordRange)]                 # -1 for 0-based
        for pair in tmpVcfList:
                indelIndex = pair[0] - min(coordRange)                                                  # pair[0] refers to the actual genomic index, but we want to find the location in this particular genome section, so we just minus the start coordinate
                if pair[1] == '.':
                        genomeSeq = genomeSeq[:indelIndex] + genomeSeq[indelIndex+1:]                   # Since pair[0] and coordRange are 1-based, minusing these results in an index that is, essentially, 0-based.
                else:
                        genomeSeq = genomeSeq[:indelIndex] + pair[1] + genomeSeq[indelIndex:]           # Because of this, we +1 to the second bit to skip the indelIndex, and leave this neutral to simply insert a base at the indel index.
        return genomeSeq

def cds_build(origCoords, newCoords, contigID, orientation, tmpVcf):
        # Build the original gene model
        origCDS = []
        for coord in origCoords:
                splitCoord = coord.split('-')
                cdsBit = str(genomeRecords[contigID].seq)[int(splitCoord[0])-1:int(splitCoord[1])]
                if orientation == '-':
                        cdsBit = reverse_comp(cdsBit)
                origCDS.append(cdsBit)
        # Build the new gene model
        newCDS = []
        prevCoord = ''
        for coord in newCoords:
                if coord == prevCoord:
                        continue                                                                        # This was validated on utg103.7 to work correctly - we get redundant coords when we naturally join two exons
                prevCoord = coord                                                                       # Hold onto this so we can find redundant coords (can happen when we have exon joinage, a single GMAP match will cover both exons)
                splitCoord = coord.split('-')
                coordRange = range(int(splitCoord[0]), int(splitCoord[1])+1)                            # Our VCF dictionary is 1-based at this point, so we want our range to act like this, too
                cdsBit = vcf_edit(tmpVcf, contigID, coordRange)
                if orientation == '-':
                        cdsBit = reverse_comp(cdsBit)
                newCDS.append(cdsBit)
        # Joing our CDS bits together
        origCDS = ''.join(origCDS)
        newCDS = ''.join(newCDS)
        return origCDS, newCDS        

def vcf_merge(vcf1, vcf2):                                                                              # This will merge vcf2 into vcf1 (currently this direction doesn't matter, but I might change this later)
        # Merge the temporary vcf into the main one
        delKeys = []                                                                                    # In the rare scenario we have a serious disagreement (one transcript says insert, another says delete at the exact same position) we'll hold onto these keys and delete them from the dictionary [this will likely never happen, but just in case]
        delContigs = []                                                                                 # We just want to hold onto the corresponding contig ID for any index keys we delete
        for key, value in vcf2.items():
                if key not in vcf1:
                        vcf1[key] = value
                else:
                        value2 = vcf1[key]
                        for k2, v2 in value.items():
                                if k2 in value2:
                                        if value2[k2] == v2:
                                                continue                                                # We don't care if it's identical, we just want to find situations that don't agree
                                        elif value2[k2] != ['.'] and v2 != ['.']:                       # I haven't seen this scenario occur, but if both transcripts agree that an insertion should occur here then we'll just stick with what we found first
                                                continue
                                        else:
                                                delKeys.append(k2)
                                                delContigs.append(key)
                                else:
                                        value2[k2] = v2
        # Delete any conflicting indel locations
        for i in range(len(delKeys)):
                del vcf1[delContigs[i]][delKeys[i]]
                # Clean up any empty dictionary keys
                if vcf1[delContigs[i]] == {}:
                        del vcf1[delContigs[i]]
        return vcf1

def vcf_output(outFileName, vcf, comment):
        if not os.path.isfile(outFileName):
                with open(outFileName, 'w') as fileOut:
                        fileOut.write('#contig_id\tposition\treplacement\n')
        with open(outFileName, 'a') as fileOut:
                if comment != '.':
                        fileOut.write(comment + '\n')
                for key, value in vcf.items():
                        value = list(value.items())
                        value.sort()
                        for pair in value:
                                fileOut.write('\t'.join([key, str(pair[0]), str(pair[1][0])]) + '\n')

def translate_cds(seq1, seq2):
        # Translate into ORFs and grab the longest bits inbetween stop codons
        records = [Seq(seq1, generic_dna), Seq(seq2, generic_dna)]
        longest = ['','']
        for i in range(len(records)):
                tmpLongest = ''
                for frame in range(3):
                        with warnings.catch_warnings():
                                warnings.simplefilter('ignore')                                         # This is just to get rid of BioPython warnings about len(seq) not being a multiple of three. We know that in two of these frames that will be true so it's not a problem.
                                frameProt = str(records[i][frame:].translate(table=1))
                        frameProt = frameProt.split('*')
                        frameProt.sort(key = len, reverse = True)
                        frameOrf = frameProt[0]
                        if len(frameOrf) > len(tmpLongest):
                                tmpLongest = frameOrf
                longest[i] = tmpLongest
        return longest

def geneblocks_update(geneBlocksDict, model, modelCoords):
        orientation = model[1]
        contigID = model[2]
        # Derive coordinates
        if orientation == '+':
                start = int(modelCoords[0].split('-')[0])
                stop = int(modelCoords[-1].split('-')[1])
        else:
                start = int(modelCoords[-1].split('-')[0])
                stop = int(modelCoords[0].split('-')[1])
        # Update geneBlocksDict
        if contigID not in geneBlocksDict:
                geneBlocksDict[contigID] = [[start, stop, model[3], orientation]]
        else:
                geneBlocksDict[contigID].append([start, stop, model[3], orientation])
        return geneBlocksDict

def gene_overlap_validation(geneBlocks):
        outlist = []
        for key, value in geneBlocks.items():
                value.sort()
                for i in range(len(value)-1):
                        if value[i][1] >= value[i+1][0]:
                                basename1 = isoRegex.search(value[i][2]).group(1)
                                basename2 = isoRegex.search(value[i+1][2]).group(1)
                                if basename1 != basename2 and value[i][3] == value[i+1][3]:             # i.e., if these aren't isoforms (basenames will be identical if they are) and the end of gene1 overlaps the start of gene 2
                                        outlist.append(value[i][2] + '\t' + value[i+1][2])
        return outlist

## CORE FUNCTIONS ##
def gmap_parse_ranges(gmapFile):
        gmapLoc = {}
        with open(gmapFile, 'r') as fileIn:
                for line in fileIn:
                        # Skip unneccessary lines
                        if line.startswith('#'):
                                continue
                        sl = line.split('\t')
                        if sl[2] != 'cDNA_match':                                                        # I don't think any other type of line is present in a GMAP gff3 file produced with PASA's settings, but this could potentially future proof the script?
                                continue
                        # Get details from line including start, stop, and orientation
                        contigID = sl[0]
                        geneID = sl[8].split(';')[1].lstrip('Name=')
                        contigStart = int(sl[3])
                        contigStop = int(sl[4])
                        indexRange = range(contigStart, contigStop+1)                                   # Make it 1-based
                        identity = int(sl[5])
                        orient = sl[6]
                        transStart = int(sl[8].split(';')[2].split()[1])
                        transStop = int(sl[8].split(';')[2].split()[2])
                        # Add to our dictionary                                                         # We index using ranges since it provides an easy way to retrieve GMAP matches by coordinates. Since these coordinates aren't unique, we filter any results returned by their contig ID.
                        if indexRange not in gmapLoc:
                                gmapLoc[indexRange] = [[contigStart, contigStop, transStart, transStop, orient, geneID, identity, contigID]]
                        else:
                                gmapLoc[indexRange].append([contigStart, contigStop, transStart, transStop, orient, geneID, identity, contigID])
        return gmapLoc

def cdna_parser(gffFile):                                                                               # I've essentially crammed the gff3_to_fasta.py script in here since we need to parse the gff3 file to get the CDS regions to perform the CDS merging and find out if we get a proper gene model
        def group_process(currGroup):
                full_mrnaGroup = []                                                                     # This will hold processed mRNA positions
                mrnaGroup = []                                                                          # This will be a temporary storage for mRNA lines
                for entry in currGroup:
                        # Handle the first line in the group: we just want the gene ID
                        if entry[2] == 'gene':
                                geneID = idRegex.search(entry[8]).group(1)
                        # Handle mRNA lines: this will start a subgroup corresponding to the mRNA
                        elif entry[2] == 'mRNA':
                                if mrnaGroup == []:                                                     # i.e., if this is the first mRNA line in this gene group, we just need to start building it
                                        mrnaGroup.append(entry)
                                else:                                                                   # i.e., there is more than one mRNA in this gene group, so we need to process the group we've built then initiate a new one
                                        # Process current mrnaGroup
                                        for subentry in mrnaGroup:
                                                if subentry[2] == 'mRNA':
                                                        full_mrnaGroup.append([idRegex.search(subentry[8]).group(1), []])
                                                elif subentry[2] == 'CDS':
                                                        coords = subentry[3] + '-' + subentry[4]        # +1 here to make Python act 1-based like gff3 format
                                                        full_mrnaGroup[-1][-1].append(coords)
                                        # Initiate new mrnaGroup
                                        full_mrnaGroup[-1] += [subentry[0],subentry[6]]                 # Append contig ID and orientation
                                        mrnaGroup = [entry]
                        else:
                                mrnaGroup.append(entry)
                # Process the mrnaGroup that's currently sitting in the pipe (so to speak)
                for subentry in mrnaGroup:
                        if subentry[2] == 'mRNA':
                                full_mrnaGroup.append([idRegex.search(subentry[8]).group(1), []])
                        elif subentry[2] == 'CDS':
                                coords = subentry[3] + '-' + subentry[4]                                # +1 here to make Python act 1-based like gff3 format
                                full_mrnaGroup[-1][-1].append(coords)
                full_mrnaGroup[-1] += [subentry[0],subentry[6]]                                         # Append contig ID and orientation
                # Put info into the coordDict and move on
                gffCoordDict[geneID] = full_mrnaGroup
                
        idRegex = re.compile(r'ID=(.+?);')
        currGroup = []
        gffCoordDict = {}
        with open(gffFile, 'r') as fileIn:
                for line in fileIn:
                        # Skip filler lines
                        if line == '\n' or line.startswith('#'):
                                continue
                        # Get details
                        sl = line.rstrip('\n').split('\t')
                        lineType = sl[2]
                        # Building gene group/process it
                        if lineType == 'gene':
                                if currGroup == []:
                                        # First iteration: just play it cool, add the sl to the group
                                        currGroup.append(sl)
                                        continue
                                else:
                                        # Process group if we're encountering a new group
                                        group_process(currGroup)
                                        currGroup = [sl]
                        elif lineType == 'rRNA' or lineType == 'tRNA':                                  # Skip lines that aren't coding
                                continue
                        else:
                                # Keep building group until we encounter another 'gene' lineType
                                currGroup.append(sl)
                # Process the last mrnaGroup
                group_process(currGroup)
        nuclDict = {}
        for key, value in gffCoordDict.items():
                for mrna in value:                                                                      # This just reorganises the results a bit. Only reason I'm doing this is because I'm reusing bits of old code, and it's easier to just take a small efficiency hit here to not bother rejigging the whole thing leading up to here.
                        nuclDict[mrna[0]] = [mrna[1], mrna[3], mrna[2], mrna[0]]
        return nuclDict

def validate_args(args):
        # Validate input file locations
        if not os.path.isfile(args.gff3File):
                print('I am unable to locate the input gff3 gene annotation file (' + args.gff3File + ')')
                print('Make sure you\'ve typed the file name or location correctly and try again.')
                quit()
        elif not os.path.isfile(args.genomeFile):
                print('I am unable to locate the input genome fasta file (' + args.genomeFile + ')')
                print('Make sure you\'ve typed the file name or location correctly and try again.')
                quit()
        elif not os.path.isfile(args.gmapFile):
                print('I am unable to locate the input GMAP transcript alignment gff3 file (' + args.gmapFile + ')')
                print('Make sure you\'ve typed the file name or location correctly and try again.')
                quit()
        elif not os.path.isfile(args.transcriptomeFile):
                print('I am unable to locate the input transcriptome fasta file (' + args.transcriptomeFile + ')')
                print('Make sure you\'ve typed the file name or location correctly and try again.')
                quit()
        # Handle file overwrites
        tmpFileName = None
        if os.path.isfile(args.outputFileName):
                if args.force:
                        # Temporarily move the file to the current directory and delete the file at the end of program run - this acts as a safety mechanism if someone actually ends up not wanting to overwrite the output file
                        tmpFileName = file_name_gen('DEGRIT_backup', '_' + os.path.basename(args.outputFileName))
                        shutil.move(args.outputFileName, tmpFileName)                                   # I'm going to do this before I alert the user since they might immediately cause a KeyboardInterrupt and I don't know what happens if you do this during shutil.move()
                        print('You\'ve specified that you want to overwrite ' + args.outputFileName)
                        print('Is that right? I\'m going to rename this file to "' + tmpFileName + '" and hold onto the file in the current directory until this program exits.')
                        print('If you don\'t want to delete this file, kill this process and you can retrieve the file.')
                else:
                        print(args.outputFileName + ' already exists. Either provide the -fo argument to this program or delete/move/rename this file and run the program again.')
                        quit()
        return tmpFileName

## Functions for optional arguments
def file_name_gen(prefix, suffix):
        ongoingCount = 2
        while True:
                if not os.path.isfile(prefix + '1' + suffix):
                        return prefix + '1' + suffix
                elif os.path.isfile(prefix + str(ongoingCount) + suffix):
                        ongoingCount += 1
                else:
                        return prefix + str(ongoingCount) + suffix

def trans_pos(sswResult):                                                                               # Function designed for assisting the logging process by providing transcript start - stop positions
        transcriptRecord = copy.deepcopy(transRecords[sswResult[7]])
        if sswResult[8] != '+':                                                                         # Put it in the same orientation as the genome sequence [the genome sequence is always + orientation]
                transcriptRecord = transcriptRecord.reverse_complement()
        transcriptBit = sswResult[0].replace('-', '')                                                   # remove hyphens since these aren't in the original record
        startpos = str(transcriptRecord.seq).find(transcriptBit) + 1                                    # +1 to make it 1-based
        endpos = str(startpos + len(transcriptBit) - 1)                                                 # -1 since our start pos is already 1-based
        transPos = str(startpos) + '-' + str(endpos)
        return transPos

def log_update(args, logName, inputList):
        if args.log:
                if not os.path.isfile(logName):
                        with open(logName, 'w') as logFile:
                                logFile.write('#contig_id\tgene_name\toriginal_exon_coords\tbest_transcript_match\ttranscript_coords_against_genome\taligned_region_of_transcript\tmodified_locations\n')
                with open(logName, 'a') as logFile:
                        # Pull out GMAP match names and coordinates
                        if inputList[3] == []:
                                matchName = '_'
                                matchCoord = '_'
                        else:
                                matchName = inputList[3][0][7]
                                matchCoord = str(inputList[3][0][5]) + '-' + str(inputList[3][0][6])
                        # Pull out the transcript alignment positions if relevant
                        transPos = '_'
                        if inputList[3] != [] and inputList[5] != '.':                                  # i.e., if inputList[3] is a sswResult list and not '.', and if inputList[5] is 'hit' and not '.' [we just specify 'hit' for the purpose of this statement]
                                transPos = trans_pos(inputList[3][0])
                        # Format the edit positions for this exon if relevant
                        editPos = '_'
                        if inputList[4] != '.':
                                editPos = ''
                                for key, value in inputList[4].items():
                                        for k2, v2 in value.items():
                                                editPos += str(k2) + ':' + v2[0] + ','
                                editPos = editPos[:-1]                                                  # Remove the last comma
                        # Write to log file
                        logFile.write('\t'.join([inputList[1][2], inputList[1][3], inputList[1][0][i], matchName, matchCoord, transPos, editPos]) + '\n')

def log_comment(args, logName, text):
        if args.log:
                if not os.path.isfile(logName):
                        with open(logName, 'w') as logFile:
                                logFile.write('#contig_id\tgene_name\toriginal_exon_coords\tbest_transcript_match\ttranscript_coords_against_genome\taligned_region_of_transcript\tmodified_locations\n')
                with open(logName, 'a') as logFile:
                        logFile.write(text + '\n')

def verbose_print(args, text):
        if args.verbose:
                print(text)

## New gene model rescuer functions
def novel_gmap_align_finder(gmapLoc, nuclDict, minCutoff):
        # Re-index our nuclDict into a format capable of comparison to our gmapLoc dictionary
        nuclRanges = {}
        for key, value in nuclDict.items():
                for coord in value[0]:
                        coordSplit = coord.split('-')
                        coordRange = range(int(coordSplit[0]), int(coordSplit[1]) + 1)                  # Make it 1-based
                        if coordRange not in nuclRanges:
                                nuclRanges[coordRange] = [[value[2], value[3]]]
                        else:
                                nuclRanges[coordRange].append([value[2], value[3]])
        # Compare gmapLoc values to nuclRanges values to find GMAP alignments which don't overlap known genes
        validExons = []
        for key, value in gmapLoc.items():
                gmapHits = copy.deepcopy(value)
                # Cull any hits that aren't good enough                                                 # It's important that we're as strict (or more) as we are with the established gene model checking
                for x in range(len(gmapHits)-1,-1,-1):
                        if gmapHits[x][6] < minCutoff:
                                del gmapHits[x]
                # Do we have enough hits to suggest there might be a defined exon here?
                if len(gmapHits) < 2:                                                                      # Because of how we indexed our GMAP alignments, we can easily tell if there are multiple sequences hitting the exact same coordinates. Convenient!
                        continue
                # Find out if this region already overlaps a known gene model
                start = min(key)
                stop = max(key)
                overlaps = [nuclRanges[key_range] for key_range in nuclRanges if start in key_range or stop in key_range]
                overlaps = copy.deepcopy(overlaps)
                # Narrow down our overlaps to hits on the same contig
                for j in range(len(overlaps)):
                        for k in range(len(overlaps[j])-1, -1, -1):
                                if overlaps[j][k][0] != gmapHits[0][7]:
                                        del overlaps[j][k]
                while [] in overlaps:
                        del overlaps[overlaps.index([])]
                # Do we have any overlaps?
                if overlaps != []:                                                                      # This list won't be empty if it overlaps an established gene model
                        continue
                else:
                        validExons.append(gmapHits)
        return validExons

def rescue_log_update(args, logName, inputList):
        if args.log:
                if not os.path.isfile(logName):
                        with open(logName, 'w') as logFile:
                                logFile.write('#contig_id\tgene_name\toriginal_exon_coords\tbest_transcript_match\ttranscript_coords_against_genome\taligned_region_of_transcript\tmodified_locations\n')
                with open(logName, 'a') as logFile:
                        # Pull out GMAP match names and coordinates
                        names, coords = [[],[]]
                        for match in inputList[1]:
                                matchName = match[7]
                                matchCoord = str(match[5]) + '-' + str(match[6])
                                names.append(matchName)
                                coords.append(matchCoord)
                        # Pull out the transcript alignment positions if relevant
                        positions = []
                        for match in inputList[1]:
                                transPos = trans_pos(match)
                                positions.append(transPos)
                        # Format the edit positions for this exon if relevant
                        editPos = '_'
                        if inputList[2] != '.':
                                editPos = ''
                                for key, value in inputList[2].items():
                                        for k2, v2 in value.items():
                                                editPos += str(k2) + ':' + v2[0] + ','
                                editPos = editPos[:-1]                                                  # Remove the last comma
                        # Write to log file
                        logFile.write('\t'.join([inputList[0], '_', '_', ','.join(names), coords[0], ','.join(positions), editPos]) + '\n')             # We just output one coords value since they're all identical

# Build regex for later use
isoRegex = re.compile(r'(evm\.model\.utg\d{1,10}(_pilon_pilon)?\.\d{1,10})')

### USER INPUT
usage = """%(prog)s aims to improve the ability to reannotate gene models. In order to work, this program
requires a gff3 file of gene annotations alongside its respective genome fasta file in addition to a gff3 file
of transcript alignments with its respective transcriptome fasta file. These files will be used to compare the
genome sequence to the aligned transcript sequence to identify any occurrences of indel errors. By correcting these
indels, reannotation of gene models can take place which will provide more accurate results.
Note: This program is designed to work with CDS regions from transcripts; this reduces the chance of falsely 
interrupting a reading frame with an edit. You can predict the CDS region using TransDecoder or EvidentialGene.
"""
# Reqs
p = argparse.ArgumentParser(description=usage)

p.add_argument("-an", "--annotation", dest="gff3File",
               help="Input gff3 gene annotation file name")
p.add_argument("-gen", "--genomefile", dest="genomeFile",
               help="Input genome contig fasta file name")
p.add_argument("-gm", "--gmap", dest="gmapFile",
               help="Input gff3 gmap transcript alignment file name")
p.add_argument("-tr", "--transfile", dest="transcriptomeFile",
               help="Input nucleotide transcriptome fasta file name (this is the same transcript file used for GMAP alignment)")
p.add_argument("-o", "--output", dest="outputFileName",
               help="Output results file name")
# Opts
p.add_argument('-r', '--rescue_genes', dest="rescue_genes", action='store_true',
               help="Optionally perform extended gene model rescue module (this is recommended)", default=False)
p.add_argument("-fo", "--force", dest="force", action='store_true',
               help="Optionally allow the program overwrite existing files at your own risk", default=False)
p.add_argument('-v', '--verbose', dest="verbose", action='store_true',
               help="Print program details to terminal", default=False)
p.add_argument('-l', '--log', dest="log", action='store_true',
               help="Additionally produce a detailed logging file as output", default=False)

args = p.parse_args()

# Validate arguments and get log file name
tmpFileName = validate_args(args)
logName = file_name_gen('DEGRIT_' + os.path.basename(args.genomeFile).rsplit('.', maxsplit=1)[0] + '_run', '.log')

# Load genome file as a dictionary
genomeRecords = SeqIO.to_dict(SeqIO.parse(open(args.genomeFile, 'rU'), 'fasta'))
verbose_print(args, 'Loaded genome fasta file')

# Parse the gff3 file
nuclDict = cdna_parser(args.gff3File)
verbose_print(args, 'Parsed the annotations gff3 file')

# Parse the gmap alignment file for transcript alignment locations
gmapLoc = gmap_parse_ranges(args.gmapFile)
verbose_print(args, 'Parsed GMAP gff3 file')

# Parse the transcriptome file
transRecords = SeqIO.to_dict(SeqIO.parse(open(args.transcriptomeFile, 'rU'), 'fasta'))
verbose_print(args, 'Loaded transcriptome fasta file')


# Declare values needed for processing
minCutoff = 98                                                                                          # I don't think this value should be modifiable - the program is built around this value, increasing it will result in finding very few results, and decreasing it will likely result in false changes
gmapCutoff = 95
"""I have two values here since, in a testing scenario, my gmap_curate function was too strict. 
Since we're checking for exon skipping now (wasn't part of the original plan but it is useful)
we want to see if there is any transcript support for the exon at all for the purpose of
providing validation information in the form of gene length increases/decreases, and the best
way to do that is to lower our gmapCutoff to see if something similar to the real exon is part of the real
gene model or not, but still use our strict cutoff for making any indel modifications.

Additionally, I am pretty sure I found a case where GMAP's identity score was noted as 97%
but in reality it was 100% identical. I've noticed a handful of weird things GMAP does (hence why I 
align my genomic exon segment against the whole transcript, GMAP's coordinates aren't trustworthy..)
so I try to limit my trust in the program's accuracy."""
vcfDict = {}                                                                                            # This dictionary will hold onto values in a style that is similar to VCF, making output and parsing easier
geneBlocks = {}                                                                                         # This dictionary serves as a form of validation. By holding onto model starts/stops, we'll be able to check for overlap which will tell us if gene models will end up merged in the reannotation.

### CORE PROCESS
verbose_print(args, '### Main gene improvement module start ###')
for key, model in nuclDict.items():
        # Hold onto both the original gene model, as well as the new gene model resulting from indel correction/exon boundary modification
        origModelCoords = []
        newModelCoords = []
        modelVcf = {}                                                                                   # This will hold onto the VCF-like dictionary for this model; we'll incorporate it into the main one if we accept these modifications
        # Scan through each individual model's exons
        for i in range(len(model[0])):
                # Find GMAP matches that align over the exon region of this coordinate
                """I'm setting up this kind of behaviour because of a situation I noticed in fragmented gene models.
                Specifically, when a gene model is fragmented, it will sometimes exceed the boundaries supported by transcript
                alignment. The result is that, when using nonexact_exon_finder(), I will not find any alignments which fully encompass
                the exon. Thus, the boundary_exon_finder will instead try to match at least one of the boundaries when we're looking at the
                first and last exons in a gene model which might not respect the positions supported by transcript evidence. I don't want to
                do this with internal exons, however, since it was causing problems that were too difficult to handle (i.e., 100% alignment
                matches to portions of the exon but not the whole exon, whereas I had other exons which perfectly matched the boundaries but
                had ~90-97% identity according to GMAP)"""
                if i == 0 or i == len(model[0]) - 1:
                        gmapMatches = gmap_exon_finder(gmapLoc, model, i, "boundary")
                else:
                        gmapMatches = gmap_exon_finder(gmapLoc, model, i, "internal")
                if gmapMatches == []:
                        origModelCoords.append(model[0][i])                                             # If there is no transcript support for this exon, it might be a spurious attempt by PASA/EVM to keep the gene inframe [this was found to be the case in utg103.43]. Thus, we'll only save these coords under the origModel
                        # Log
                        log_update(args, logName, [key, model, i, gmapMatches, '.', '.'])
                        continue
                gmapMatches = gmap_curate(minCutoff, gmapMatches, model, i)
                # Continue if no GMAP matches
                if gmapMatches == []:
                        origModelCoords.append(model[0][i])
                        newModelCoords.append(model[0][i])                                              # In this case, there IS transcript support for this exon, but it's not good enough for us to make edits with. Thus, we'll just hold onto the original coordinates for our new model.
                        # Log
                        log_update(args, logName, [key, model, i, gmapMatches, '.', '.'])
                        continue
                # Find the best GMAP match by SSW alignment score
                sswResults = []
                for match in gmapMatches:
                        # Grab the sequences for alignment                                              # Note that we're going to compare the portion of the genome which the transcript hits (from GMAP) to the full transcript since GMAP handles N's weirdly and thus its transcript coordinates cannot be used
                        transcriptRecord, genomePatchRec = patch_seq_extract(match, model)
                        # Perform SSW alignment
                        sswResults.append(ssw(genomePatchRec, transcriptRecord) + [match[0], match[1], match[5], match[4]])  # SSW returns [transcriptAlign, genomeAlign, hyphen, startIndex, alignment.optimal_alignment_score), and we also + [matchStart, matchEnd, matchName, matchOrientation] to this
                sswResults.sort(key = lambda x: (-x[4], x[2], x[3]))                                    # i.e., sort so score is maximised, then sort by presence of hyphens then by the startIndex
                # Look at our best match to see if indels are present
                if sswResults[0][2] == 'n':                                                             # i.e., if we have no hyphens in our alignment, then there are no indels
                        origModelCoords.append(model[0][i])
                        newModelCoords.append(str(sswResults[0][5]) + '-' + str(sswResults[0][6]))      # Despite the fact that no indels are present, it's possible that the exon boundaries should change to fit with other indels. Thus, we'll use the exon boundaries suggested by SSW.
                        # Log
                        log_update(args, logName, [key, model, i, sswResults, '.', 'hit'])
                else:
                        # Modify our modelVcf if the alignment is trustworthy
                        modelVcf, sswIdentity, tmpVcf = indel_location(sswResults[0][0], sswResults[0][1], sswResults[0][5], model, sswResults[0][3], modelVcf, minCutoff)   # This will update our vcfDict with indel locations
                        if sswIdentity >= minCutoff:
                                origModelCoords.append(model[0][i])
                                newModelCoords.append(str(sswResults[0][5]) + '-' + str(sswResults[0][6]))
                                # Log
                                log_update(args, logName, [key, model, i, sswResults, tmpVcf, 'hit'])
                        else:
                                origModelCoords.append(model[0][i])
                                newModelCoords.append(model[0][i])                                      # Like above after gmap_curate, there is transcript support for this exon. Here, we chose not to make any changes, so we'll stick to the original coordinates.
                                # Log
                                log_update(args, logName, [key, model, i, sswResults, '.', 'hit'])
        # Hold onto any indel positions and provide logging information about this
        if modelVcf == {}:
                geneBlocks = geneblocks_update(geneBlocks, model, origModelCoords)
                # Verbose and log
                verbose_print(args, 'Found no edits [' + model[3] + ']')
                log_comment(args, logName, '#' + model[3] + '\tNo edits found')
        else:
                origCDS, newCDS = cds_build(origModelCoords, newModelCoords, model[2], model[1], modelVcf)
                origProt, newProt = translate_cds(origCDS, newCDS)
                # Is the newCDS at least as long as the original CDS without internal stop codons?
                if len(newProt) > len(origProt):
                        vcfDict = vcf_merge(vcfDict, modelVcf)
                        geneBlocks = geneblocks_update(geneBlocks, model, newModelCoords)
                        # Verbose and log
                        verbose_print(args, 'Looks like we improved this model! [' + model[3] + ']')
                        log_comment(args, logName, '#' + model[3] + '\tModel length increased\tOld=' + origProt + '\tNew=' + newProt)
                elif len(newProt) == len(origProt):
                        vcfDict = vcf_merge(vcfDict, modelVcf)
                        geneBlocks = geneblocks_update(geneBlocks, model, newModelCoords)
                        # Verbose and log
                        verbose_print(args, 'Length is the same, I\'ll save changes though. [' + model[3] + ']')
                        log_comment(args, logName, '#' + model[3] + '\tModel length is the same\tOld=' + origProt + '\tNew=' + newProt)
                else:
                        # Check how much shorter the new model is
                        if len(newProt) / len(origProt) >= 0.90:
                                """This check is in place for the same reasons as mentioned above about exon skipping. Sometimes the real gene model should have 
                                a skipped exon (since EVM/PASA will add in a spurious one to maintain a reading frame in the presence of indel error) which means
                                our newProt will be slightly shorter than origProt and that is not cause for alarm."""
                                vcfDict = vcf_merge(vcfDict, modelVcf)
                                geneBlocks = geneblocks_update(geneBlocks, model, newModelCoords)
                                # Verbose and log
                                verbose_print(args, 'We shortened this model, but not by much. It\'s probably skipping an exon [' + model[3] + ']')
                                log_comment(args, logName, '#' + model[3] + '\tModel length is _slightly_ shorter\tOld=' + origProt + '\tNew=' + newProt)
                        else:
                                """After testing this program I'm pretty confident that scenarios where this occurs are likely to indicate 1) chimerism that occurred as a result of
                                indel error, or 2) gene models that are a mix of exons supported by transcripts and exons predicted ab initio. When running this on the test dataset,
                                it handles all scenarios fine until a gene model marked as MERGED by PASA. Performing BLAST makes it clear that this join was incorrect, and when we fixed indels with
                                this program, a continuous ORF was not possible between these two genes. Thus, although this gene model did get shorter, this was a good thing.
                                Originally, I was not going to make any modifications that did shorten the gene model. However, an extensive set of internal validations and rough heuristics are built 
                                into the program and I have tested it to a degree that I am confident enough to unlock this section and let the program make the edits it thinks it should. 
                                I will, however, make these cases clear in the logging function so that manual validation can occur to make sure it's not messing up any gene models, 
                                which is the #1 goal of this program - do not make _anything_ worse."""
                                vcfDict = vcf_merge(vcfDict, modelVcf)
                                geneBlocks = geneblocks_update(geneBlocks, model, newModelCoords)
                                # Verbose and log
                                verbose_print(args, 'I shortened this model a lot. Was this gene a chimer, or does it involve ab initio predicted exons? [' + model[3] + ']')
                                log_comment(args, logName, '#' + model[3] + '\tModel length is _quite a bit_ shorter\tOld=' + origProt + '\tNew=' + newProt)

# Check for probable gene joins
if args.verbose or args.log:
        joins = gene_overlap_validation(geneBlocks)
        verbose_print(args, '### Probable gene merges ###\n' + '\n'.join(joins))
        log_comment(args, logName, '### Probable gene merges ###\n' + '\n'.join(joins))
        """This function is only capable of finding gene models that join over shared exons. Gene models that join 
        through introns should be discovered by EVM/PASA but it's too difficult/annoying to try to find these scenarios
        in this program. This at least provides a nice way to validate these changes using manual annotation to see if things
        are going according to plan"""

# Create output VCF-like file                                                                                   # It's a really abbreviated VCF style format, but it's enough to make it easy to parse and perform genome edits with a downstream script
vcf_output(args.outputFileName, vcfDict, '.')

# Gene model rescuer module
if args.rescue_genes:
        verbose_print(args, '### Gene model rescue module start ###')
        log_comment(args, logName, '### Gene model rescue exon indels ###')
        gmapHits = novel_gmap_align_finder(gmapLoc, nuclDict, minCutoff)
        novelVcf = {}
        for hit in gmapHits:
                # Find the best GMAP match by SSW alignment score
                sswResults = []
                for match in hit:
                        model = ['','',match[7]]                                                                # We're going to just hijack the functions developed for the main part of the program where possible
                        # Grab the sequences for alignment                                                      # Note that we're going to compare the portion of the genome which the transcript hits (from GMAP) to the full transcript since GMAP handles N's weirdly and thus its transcript coordinates cannot be used
                        transcriptRecord, genomePatchRec = patch_seq_extract(match, model)
                        # Perform SSW alignment
                        sswResults.append(ssw(genomePatchRec, transcriptRecord) + [match[0], match[1], match[5], match[4]])  # SSW returns [transcriptAlign, genomeAlign, hyphen, startIndex, alignment.optimal_alignment_score), and we also + [matchStart, matchEnd, matchName, matchOrientation] to this
                sswResults.sort(key = lambda x: (-x[4], x[2], x[3]))                                            # i.e., sort so score is maximised, then sort by presence of hyphens then by the startIndex
                # Look at our best match to see if indels are present
                if sswResults[0][2] == 'n':                                                                     # i.e., if we have no hyphens in our alignment, then there are no indels
                        # Log
                        rescue_log_update(args, logName, [match[7], sswResults, '.'])
                        continue        # Need to fix the logging here
                elif sswResults[0][2] != 'n' and sswResults[1][2] == 'n':                                       # We want unanimous agreement since we have to be a bit more strict when we're not working with known exon boundaries
                        # Log
                        rescue_log_update(args, logName, [match[7], sswResults, '.'])
                        continue
                else:
                        # See if the indels are located in the exact same position by all good alignments
                        indelLocations = []
                        firstVcf = ''
                        same = 'y'
                        for result in sswResults:
                                modelVcf, sswIdentity, tmpVcf = indel_location(result[0], result[1], result[5], model, result[3], modelVcf, minCutoff)   # This will update our vcfDict with indel locations
                                if sswIdentity >= minCutoff:
                                        # Get the tmpVcf locations (i.e., keys) into a list for comparison
                                        indelLocations.append(set(tmpVcf[match[7]].keys()))
                                        # Save our first/best VCF dict
                                        if firstVcf == '':
                                                firstVcf = copy.deepcopy(tmpVcf)
                        for x in range(0, len(indelLocations)-1):
                                if indelLocations[x] == indelLocations[x+1]:
                                        continue
                                else:
                                        same = 'n'
                                        break
                        if same == 'y':
                                # Since we have multiple alignments all agreeing on the exact same location of the indel(s), we are pretty happy that this indel is genuine
                                novelVcf = vcf_merge(novelVcf, firstVcf)
                                rescue_log_update(args, logName, [match[7], sswResults, firstVcf])
        # Output to VCF
        vcf_output(args.outputFileName, novelVcf, '# Gene rescue module indel predictions')

# Remove the output file that was being replaced if relevant
if tmpFileName != None:
        os.remove(tmpFileName)

#### SCRIPT ALL DONE
